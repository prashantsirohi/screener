"""
Is the EAV load lossless?

The fundamentals import explodes each page into ~440 fact rows. If that explosion
drops or mangles anything, every downstream metric is quietly wrong. The check is
a round trip: rebuild the payload from screener_fact and compare it to the raw
payload retained on screener_page_raw.
"""

from __future__ import annotations

import pytest

from market_screener.config import load_settings
from market_screener.db.connection import Database
from market_screener.domain import fundamentals_view as fv

pytestmark = pytest.mark.integration

STATEMENTS = ("profit_loss", "balance_sheet", "cash_flow", "ratios",
              "quarters", "shareholding")


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
def sample(db):
    """A deterministic spread of companies, biased toward data-rich ones."""
    return db.fetch_all("""
        SELECT s.security_id, s.symbol
        FROM   market.security s
        JOIN   market.screener_page_raw p ON p.security_id = s.security_id
        WHERE  NOT p.is_blank
        ORDER  BY s.symbol
        LIMIT  60
    """)


def test_reconstruction_matches_raw_payload(db, labels, sample):
    """
    Exact comparison, nulls included.

    An earlier version stripped nulls from the expected side, which hid the fact
    that the loader was dropping them. That is not cosmetic: `series[-1]` returns
    the last *listed* period, so a blank latest year silently became the last
    non-blank one and moved opm_latest_pct and inventory_days.
    """
    mismatches = []
    for row in sample:
        raw = fv.raw_payload(db, row["security_id"])
        rebuilt = fv.reconstruct_payload(db, row["security_id"], labels=labels)

        for stmt in STATEMENTS:
            want = {k: dict(v) for k, v in (raw.get(stmt) or {}).items() if v}
            got = rebuilt.get(stmt) or {}
            if want != got:
                differing = [k for k in want if got.get(k) != want[k]]
                mismatches.append((row["symbol"], stmt, differing[:4]))

    assert not mismatches, f"EAV round trip lost data: {mismatches[:6]}"


def test_growth_block_round_trips(db, labels, sample):
    """
    The compounded-growth tables feed screener_sales_cagr_5y, stock_cagr_5y and
    friends. The loader skipped the block entirely at first and the round-trip
    test did not look at it, so the loss only surfaced in metrics parity.
    """
    mismatches = []
    for row in sample:
        raw = fv.raw_payload(db, row["security_id"]) or {}
        rebuilt = fv.reconstruct_payload(db, row["security_id"], labels=labels)
        want = {k: dict(v) for k, v in (raw.get("growth") or {}).items() if v}
        got = rebuilt.get("growth") or {}
        if want != got:
            mismatches.append((row["symbol"], sorted(set(want) ^ set(got))[:4]))
    assert not mismatches, f"growth block lost: {mismatches[:6]}"


def test_nulls_are_preserved_not_dropped(db, labels, sample):
    """At least one company must carry an explicitly-null period, proving the
    loader stores them rather than omitting the key."""
    found_null = False
    for row in sample:
        rebuilt = fv.reconstruct_payload(db, row["security_id"], labels=labels)
        for stmt in STATEMENTS:
            for series in (rebuilt.get(stmt) or {}).values():
                if any(v is None for v in series.values()):
                    found_null = True
                    break
    assert found_null, "no null-valued periods survived the round trip"


def test_top_ratios_round_trip(db, labels, sample):
    """Exact, nulls included - a reported-but-blank ratio is not the same as an
    absent one."""
    bad = []
    for row in sample:
        raw = fv.raw_payload(db, row["security_id"])
        want = dict(raw.get("top_ratios") or {})
        got = rebuilt = fv.reconstruct_payload(
            db, row["security_id"], labels=labels)["top_ratios"]
        if want != got:
            missing = {k: want[k] for k in want if k not in got or got[k] != want[k]}
            extra = {k: got[k] for k in got if k not in want}
            bad.append((row["symbol"], missing, extra))
    assert not bad, f"top_ratios round trip failed: {bad[:3]}"


def test_as_of_filter_hides_later_knowledge(db, labels, sample):
    """A cutoff before anything was scraped must yield an empty payload."""
    from datetime import datetime, timezone
    sid = sample[0]["security_id"]
    early = datetime(2000, 1, 1, tzinfo=timezone.utc)
    rebuilt = fv.reconstruct_payload(db, sid, as_of=early, labels=labels)
    assert all(not rebuilt[s] for s in STATEMENTS)
    assert not rebuilt["top_ratios"]


def test_blank_pages_report_as_blank(db):
    """
    Picks a security whose LATEST page is still blank. Once the retry queue
    recovers a symbol it has a newer good page, and payload_for_metrics correctly
    returns that instead - so the fixture must select on the latest page, not on
    any blank page ever recorded.
    """
    sid = db.fetch_value("""
        SELECT security_id FROM (
            SELECT DISTINCT ON (security_id) security_id, is_blank
            FROM   market.screener_page_raw
            ORDER  BY security_id, fetched_at DESC
        ) latest
        WHERE is_blank LIMIT 1
    """)
    if sid is None:
        pytest.skip("no securities remain blank; the retry queue drained them all")
    rec = fv.payload_for_metrics(db, sid)
    assert rec.get("error") == "blank_page"


def test_recovered_securities_expose_their_new_payload(db):
    """After recovery the reconstruction must serve the good page, not the shell."""
    sid = db.fetch_value("""
        SELECT s.security_id
        FROM   market.fetch_retry_queue q
        JOIN   market.security s ON s.symbol = q.scope AND s.exchange = 'NSE'
        WHERE  q.state = 'resolved' LIMIT 1
    """)
    if sid is None:
        pytest.skip("nothing recovered yet")
    rec = fv.payload_for_metrics(db, sid)
    assert "error" not in rec
    assert rec["top_ratios"], "a recovered page should carry top ratios"


def test_period_labels_render_the_way_the_parser_wrote_them(db):
    from datetime import date
    assert fv.period_label(date(2026, 3, 31), "annual") == "Mar 2026"
    assert fv.period_label(date(2025, 6, 30), "quarter") == "Jun 2025"
    assert fv.period_label(date(2026, 3, 31), "ttm") == "TTM"
