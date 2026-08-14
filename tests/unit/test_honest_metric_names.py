"""
Metric names must not claim more than the data supports.

`net_debt_to_equity` was gross borrowings over equity, because the aggregator
does not report cash. `normalized_eps_cagr_5y_pct` is reported EPS with
exceptional items in it. Both named a number the system has never had.

The internal names are now honest and the frozen CSV headers are unchanged, so
the risk is a half-finished rename: one surviving `m.get("net_debt_to_equity")`
returns None silently, and a leverage test that never fires looks exactly like a
company with no debt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from market_screener.domain.metrics import LEGACY_CSV_NAMES

SRC = Path(__file__).resolve().parents[2] / "src" / "market_screener"

# Where the old names are still legitimate: the CSV contract itself.
CONTRACT_FILES = {
    "s80_phase1_screen.py",   # the 37-column header tuple and staging types
    "s85_phase1_outputs.py",  # candidate CSV column order
    "s90_summary.py",         # the data dictionary explaining the discrepancy
    "runs.py",                # `runs diff` selects by column name
    "metrics.py",             # LEGACY_CSV_NAMES itself
}

RENAMED = {
    "net_debt_to_equity": "gross_debt_to_equity",
    "eps_cagr_5y_pct": "reported_eps_cagr_5y_pct",
    "eps_cagr_3y_pct": "reported_eps_cagr_3y_pct",
}


def python_sources():
    for p in SRC.rglob("*.py"):
        yield p


def test_no_consumer_still_reads_the_old_metric_names():
    """
    A missed call site does not raise - it reads None and quietly changes a
    score. Restricted to `.get("...")` so column-name strings in the contract
    files are not confused with metric lookups.
    """
    offenders = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        for old in RENAMED:
            for form in (f'.get("{old}")', f"['{old}']", f'["{old}"]'):
                if form in text:
                    offenders.append(f"{path.name}: {form}")
    assert not offenders, (
        "these still read the pre-rename metric names, which now return None:\n  "
        + "\n  ".join(offenders))


def test_the_renamed_metrics_are_actually_emitted():
    """The rename is only safe if the new keys exist. Guards a typo'd rename."""
    import inspect

    from market_screener.domain import metrics

    src = inspect.getsource(metrics.compute)
    for new in RENAMED.values():
        assert f'out["{new}"]' in src, f"{new} is never set"


def test_old_names_survive_only_in_the_contract_layer():
    """
    They are legitimate as CSV headers. If one appears anywhere else, the
    boundary has leaked and something is writing the honest name into a frozen
    column or vice versa.
    """
    leaks = []
    for path in python_sources():
        if path.name in CONTRACT_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for old in ("net_debt_to_equity", "normalized_eps_cagr_5y_pct"):
            if old in text:
                leaks.append(f"{path.name}: {old}")
    assert not leaks, "old names outside the contract layer:\n  " + "\n  ".join(leaks)


@pytest.mark.parametrize("internal,column", sorted(LEGACY_CSV_NAMES.items()))
def test_the_mapping_is_documented_in_one_place(internal, column):
    assert internal != column
    assert internal.startswith(("gross_", "reported_"))


def test_the_csv_contract_is_unchanged():
    """
    The whole point of renaming internals only. If the 37-column header moved,
    Phase 2 and the parity baseline both break.
    """
    from market_screener.pipeline.stages.s85_phase1_outputs import SCREEN_COLS

    assert len(SCREEN_COLS) == 37, "the 37-column contract changed width"
    assert "net_debt_to_equity" in SCREEN_COLS
    assert "normalized_eps_cagr_5y_pct" in SCREEN_COLS
    for internal in LEGACY_CSV_NAMES:
        assert internal not in SCREEN_COLS, \
            f"internal name {internal} leaked into the frozen CSV contract"
