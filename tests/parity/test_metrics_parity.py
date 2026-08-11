"""
Metrics parity: the ported pipeline must produce the same numbers as the frozen
baseline.

`domain/metrics.py` is a byte-identical copy of the oracle, so the risk is not
the arithmetic - it is the data path. The legacy screen read a JSON file straight
off disk; the port reads ~440 rows back out of an EAV fact table and rebuilds the
payload. This asserts the two produce identical metric bundles, field for field,
across a 60-company golden set.

If this fails, the store lost or reshaped something and every downstream number
is suspect.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

from market_screener.config import load_settings
from market_screener.db.connection import Database
from market_screener.domain import metrics as ported
from market_screener.domain import fundamentals_view as fv

pytestmark = pytest.mark.parity

CACHE = Path("data/fundamentals")
GOLDEN_SIZE = 60
FLOAT_TOL = 1e-9


def _load_reference():
    """Import the frozen oracle by path so it can never drift with the package."""
    p = Path(__file__).resolve().parents[1] / "reference" / "legacy_metrics.py"
    spec = importlib.util.spec_from_file_location("legacy_metrics", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


legacy = _load_reference()


@pytest.fixture(scope="module")
def db():
    st = load_settings()
    d = Database(st.pg)
    if not d.database_exists() or not d.ping():
        pytest.skip("market_screener database unavailable")
    if (d.fetch_value("SELECT count(*) AS c FROM market.screener_fact") or 0) == 0:
        pytest.skip("store not populated")
    return d


@pytest.fixture(scope="module")
def labels(db):
    return fv._label_lookup(db)


@pytest.fixture(scope="module")
def golden(db):
    """
    Deterministic set of companies whose cached page is still the only one.

    Symbols recovered by the retry queue have a NEWER page in the store than the
    JSON on disk, so comparing them would measure the recovery, not the port.
    """
    rows = db.fetch_all("""
        SELECT s.security_id, s.symbol, s.nse_industry
        FROM   market.security s
        JOIN   market.screener_page_raw p ON p.security_id = s.security_id
        WHERE  NOT p.is_blank
          AND  s.series = 'EQ' AND s.is_active
          AND  NOT EXISTS (
                SELECT 1 FROM market.fetch_retry_queue q
                WHERE q.scope = s.symbol AND q.state = 'resolved')
          AND  (SELECT count(*) FROM market.screener_page_raw p2
                WHERE p2.security_id = s.security_id) = 1
        ORDER  BY s.symbol
        LIMIT  %s
    """, (GOLDEN_SIZE,))
    if len(rows) < GOLDEN_SIZE:
        pytest.skip(f"only {len(rows)} comparable companies available")
    return rows


def _same(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(float(a)) and math.isnan(float(b)):
            return True
        scale = max(1.0, abs(float(a)), abs(float(b)))
        return abs(float(a) - float(b)) <= FLOAT_TOL * scale
    return a == b


# Fields that legitimately differ: they describe where the data came from, not
# what it says.
PROVENANCE_FIELDS = {"source_url", "basis", "symbol", "company", "industry"}


def test_golden_set_is_the_expected_size(golden):
    assert len(golden) == GOLDEN_SIZE


def test_metric_bundles_match_field_for_field(db, labels, golden):
    failures: list[str] = []
    compared_fields = 0

    for row in golden:
        sym, sid, industry = row["symbol"], row["security_id"], row["nse_industry"]

        raw = json.loads((CACHE / f"{sym}.json").read_text(encoding="utf-8"))
        want = legacy.compute(raw, industry)

        rebuilt = fv.payload_for_metrics(db, sid, labels=labels)
        got = ported.compute(rebuilt, industry)

        keys = (set(want) | set(got)) - PROVENANCE_FIELDS
        for k in sorted(keys):
            compared_fields += 1
            if not _same(want.get(k), got.get(k)):
                failures.append(f"{sym}.{k}: legacy={want.get(k)!r} port={got.get(k)!r}")

    assert compared_fields > GOLDEN_SIZE * 50, \
        f"only {compared_fields} fields compared; the bundle looks truncated"
    assert not failures, (
        f"{len(failures)} field mismatches across {len(golden)} companies:\n  "
        + "\n  ".join(failures[:25]))


def test_headline_metrics_are_actually_populated(db, labels, golden):
    """Guards against a vacuous pass where both sides return all-None."""
    headline = ["market_cap_inr_cr", "revenue_cagr_5y_pct", "roce_median_5y_pct",
                "cfo_pat_5y", "promoter_holding_pct", "net_debt_to_equity"]
    populated = {k: 0 for k in headline}
    for row in golden:
        got = ported.compute(
            fv.payload_for_metrics(db, row["security_id"], labels=labels),
            row["nse_industry"])
        for k in headline:
            if got.get(k) is not None:
                populated[k] += 1
    for k, n in populated.items():
        assert n >= GOLDEN_SIZE * 0.5, \
            f"{k} populated for only {n}/{len(golden)} - the port is losing data"


def test_financial_detection_survives_the_port(db, labels, golden):
    """
    Lenders are detected by a 'Financing Profit' row. Aliasing that label away
    would silently reclassify every bank and then apply CFO/PAT and debt/equity
    tests that mean nothing for them.
    """
    for row in golden:
        raw = json.loads((CACHE / f"{row['symbol']}.json").read_text(encoding="utf-8"))
        want = legacy.compute(raw, row["nse_industry"])
        got = ported.compute(
            fv.payload_for_metrics(db, row["security_id"], labels=labels),
            row["nse_industry"])
        assert want["is_financial"] == got["is_financial"], row["symbol"]


def test_point_in_time_query_reproduces_the_original_scrape(db, labels, golden):
    """
    An as-of query must return the numbers as they were known then, not as they
    were later restated. Asking as of the original scrape time must reproduce the
    original bundle exactly.
    """
    row = golden[0]
    scraped = db.fetch_value(
        "SELECT max(fetched_at) AS m FROM market.screener_page_raw WHERE security_id = %s",
        (row["security_id"],))
    at_scrape = ported.compute(
        fv.payload_for_metrics(db, row["security_id"], as_of=scraped, labels=labels),
        row["nse_industry"])
    latest = ported.compute(
        fv.payload_for_metrics(db, row["security_id"], labels=labels),
        row["nse_industry"])
    for k in set(at_scrape) - PROVENANCE_FIELDS:
        assert _same(at_scrape.get(k), latest.get(k)), k
