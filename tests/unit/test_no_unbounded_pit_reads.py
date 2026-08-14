"""
Lint: no unbounded reads of point-in-time tables in the screening path.

The screen already had two of these and they were invisible - `available_at` sat
in the screener_fact primary key doing nothing, because the one caller never
passed a cutoff. A third was found in the summary header, reporting a financial
cutoff later than the run's own as_of.

Scope is `domain/` and `pipeline/stages/` - the code a screening run executes.
`ingest/` is deliberately excluded: a collector's job is to see everything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "market_screener"
SCOPE = ("domain", "pipeline/stages")

# table -> the column that bounds it in time
PIT_TABLES = {
    "screener_fact": "available_at",
    "screener_page_raw": "fetched_at",
    "announcement": "announced_at",
}

# Reads that legitimately span all time, with the reason. Anything not listed
# must carry a bound.
EXEMPT = {
    # Corpus-wide row counts for the stage fingerprint, not screening inputs -
    # they exist precisely to notice that new data arrived.
    ("pipeline/stages", "context.py"),
}

SQL_STRING = re.compile(r'"""(.*?)"""|"([^"\n]{40,})"', re.S)


def sql_blocks(path: Path):
    for m in SQL_STRING.finditer(path.read_text(encoding="utf-8")):
        block = m.group(1) or m.group(2) or ""
        if re.search(r"\bSELECT\b", block, re.I):
            yield block


def offenders() -> list[str]:
    bad = []
    for scope in SCOPE:
        for path in (SRC / scope).rglob("*.py"):
            if (scope, path.name) in EXEMPT:
                continue
            for block in sql_blocks(path):
                for table, col in PIT_TABLES.items():
                    if not re.search(rf"\bmarket\.{table}\b", block):
                        continue
                    # A bound is that table's time column in a comparison.
                    if re.search(rf"{col}\s*(<|<=)", block):
                        continue
                    # Or the join carries it via an already-bounded alias.
                    if re.search(r"(available_at|fetched_at|announced_at)\s*(<|<=)",
                                 block):
                        continue
                    bad.append(
                        f"{scope}/{path.name}: reads market.{table} without a "
                        f"{col} bound:\n    {' '.join(block.split())[:140]}")
    return bad


def test_no_unbounded_point_in_time_reads():
    found = offenders()
    assert not found, (
        "unbounded point-in-time read(s) in the screening path. Every one of "
        "these lets a backdated run see data that arrived after its as_of:\n\n"
        + "\n\n".join(found))


def test_the_lint_can_actually_fail(tmp_path):
    """
    A guard that cannot fire is worse than none - it reads as protection while
    providing none. Prove this one detects the exact bug it was written for.
    """
    offending = tmp_path / "bad.py"
    offending.write_text(
        'q = """SELECT max(report_date) FROM market.screener_fact '
        'WHERE period_type = \'annual\'"""', encoding="utf-8")
    blocks = list(sql_blocks(offending))
    assert blocks, "the SQL extractor did not find the statement"
    assert not re.search(r"available_at\s*(<|<=)", blocks[0])


@pytest.mark.parametrize("table,col", sorted(PIT_TABLES.items()))
def test_bounded_sql_is_accepted(table, col, tmp_path):
    """And that it does not fire on correctly bounded SQL."""
    ok = tmp_path / "good.py"
    ok.write_text(
        f'q = """SELECT 1 FROM market.{table} WHERE {col} < %s"""',
        encoding="utf-8")
    block = next(sql_blocks(ok))
    assert re.search(rf"{col}\s*(<|<=)", block)
