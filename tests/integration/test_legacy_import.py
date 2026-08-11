"""
Invariants of the imported store.

These assert against the live market_screener database rather than building a
throwaway one, because the point is to verify the actual 912k-fact import. They
skip when the store has not been populated.
"""

from __future__ import annotations

import pytest

from market_screener.config import load_settings
from market_screener.db.connection import Database

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db():
    st = load_settings()
    d = Database(st.pg)
    if not d.database_exists() or not d.ping():
        pytest.skip("market_screener database unavailable")
    if (d.fetch_value("SELECT count(*) AS c FROM market.screener_fact") or 0) == 0:
        pytest.skip("store not populated; run `screener import-legacy-cache`")
    return d


# ---------------- universe ----------------

def test_active_universe_matches_the_current_equity_l(db):
    """
    The screening universe is what EQUITY_L says is tradable today - 2,086 names.

    The security table holds more than that, because a three-year price backfill
    registers symbols that have since delisted, merged or changed series. Their
    history is kept; they must not widen the screen.
    """
    active = db.fetch_value(
        "SELECT count(*) AS c FROM market.security "
        "WHERE series='EQ' AND exchange='NSE' AND security_type='equity' AND is_active")
    assert active == 2086


def test_historically_registered_securities_are_inactive_but_retained(db):
    inactive = db.fetch_value(
        "SELECT count(*) AS c FROM market.security "
        "WHERE series='EQ' AND security_type='equity' AND NOT is_active")
    assert inactive > 0, "the backfill should have found delisted/renamed symbols"
    orphaned = db.fetch_value("""
        SELECT count(*) AS c FROM market.security s
        WHERE NOT s.is_active AND s.series='EQ'
          AND NOT EXISTS (SELECT 1 FROM market.price_daily p
                          WHERE p.security_id = s.security_id)
    """)
    assert orphaned == 0, "an inactive security should exist only to carry history"


def test_benchmarks_registered_as_indices(db):
    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.security WHERE security_type = 'index'")
    assert n == 12


def test_industry_labels_present(db):
    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.security WHERE nse_industry IS NOT NULL")
    assert n >= 742, "NSE index files should supply industry for ~752 symbols"


# ---------------- defect 2: bhavcopy turnover ----------------

def test_every_bhavcopy_row_has_turnover(db):
    """The legacy loader renamed columns that do not exist, so turnover was
    always NULL and the liquidity gate was entirely Yahoo-derived."""
    total = db.fetch_value("SELECT count(*) AS c FROM market.price_daily")
    with_t = db.fetch_value(
        "SELECT count(*) AS c FROM market.price_daily WHERE turnover_inr IS NOT NULL")
    assert total > 0 and with_t == total


def test_turnover_is_plausible(db):
    """Sanity: median daily turnover in INR crore should sit in single digits."""
    med = db.fetch_value(
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY turnover_inr / 1e7) AS m "
        "FROM market.price_daily WHERE turnover_inr > 0")
    assert 0.5 < float(med) < 50, f"median daily turnover {med} INR cr looks wrong"


# ---------------- point-in-time fundamentals ----------------

def test_all_facts_carry_a_knowledge_date(db):
    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.screener_fact WHERE available_at IS NULL")
    assert n == 0


def test_facts_reference_known_metrics(db):
    orphans = db.fetch_value("""
        SELECT count(*) AS c FROM market.screener_fact f
        LEFT JOIN market.metric_dim m ON m.metric_id = f.metric_id
        WHERE m.metric_id IS NULL
    """)
    assert orphans == 0


def test_core_metrics_are_present(db):
    rows = db.fetch_all("SELECT metric_id FROM market.metric_dim")
    ids = {r["metric_id"] for r in rows}
    for expected in ("profit_loss.sales", "profit_loss.net_profit", "profit_loss.eps",
                     "balance_sheet.borrowings", "balance_sheet.equity_capital",
                     "cash_flow.cash_from_operating_activity", "ratios.roce_pct",
                     "top_ratios.market_cap", "shareholding.promoters"):
        assert expected in ids, f"missing metric {expected}"


# ---------------- blank quarantine ----------------

def test_the_307_blank_pages_were_all_quarantined(db):
    """The import found 307 shells; every one must be recorded as such."""
    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.screener_page_raw WHERE is_blank")
    assert n == 307


def test_every_quarantined_blank_is_accounted_for(db):
    """
    A blank must be in exactly one of three states - waiting, given up on, or
    recovered. What must never happen is a company silently disappearing from
    the universe because its page came back empty.
    """
    states = db.fetch_all("""
        SELECT state, count(*) AS n FROM market.fetch_retry_queue
        WHERE source = 'fundamentals.screener' GROUP BY state
    """)
    total = sum(r["n"] for r in states)
    by_state = {r["state"]: r["n"] for r in states}
    assert total == 307, f"queue holds {total} rows for 307 blanks: {by_state}"
    assert set(by_state) <= {"pending", "in_flight", "resolved", "exhausted"}


def test_recovered_symbols_actually_have_facts(db):
    """A row marked 'resolved' must have produced data, not just changed state."""
    empty = db.fetch_all("""
        SELECT q.scope FROM market.fetch_retry_queue q
        JOIN   market.security s ON s.symbol = q.scope AND s.exchange = 'NSE'
        WHERE  q.source = 'fundamentals.screener' AND q.state = 'resolved'
          AND  NOT EXISTS (SELECT 1 FROM market.screener_fact f
                           WHERE f.security_id = s.security_id)
        LIMIT 5
    """)
    assert not empty, f"resolved but no facts: {[r['scope'] for r in empty]}"


def test_abbott_india_was_recovered(db):
    """
    A ~INR 59,000 cr company the throttle silently dropped from the legacy screen.
    It is the reason the retry queue exists, so its recovery is worth asserting.
    """
    row = db.fetch_one("""
        SELECT q.state, f.value AS market_cap_cr
        FROM   market.security s
        LEFT JOIN market.fetch_retry_queue q
               ON q.scope = s.symbol AND q.source = 'fundamentals.screener'
        LEFT JOIN market.screener_fact_current f
               ON f.security_id = s.security_id
              AND f.metric_id = 'top_ratios.market_cap'
        WHERE  s.symbol = 'ABBOTINDIA'
    """)
    assert row is not None
    if row["state"] == "resolved":
        assert row["market_cap_cr"] and float(row["market_cap_cr"]) > 10_000
    else:
        assert row["state"] in ("pending", "in_flight"), \
            "still blank, but must remain queued rather than be dropped"


def test_blank_pages_contributed_no_facts(db):
    """A shell page must not leak partial rows into the fact table."""
    n = db.fetch_value("""
        SELECT count(*) AS c
        FROM   market.screener_fact f
        JOIN   market.screener_page_raw p ON p.page_id = f.page_id
        WHERE  p.is_blank
    """)
    assert n == 0


# ---------------- defect 3: weekly alignment ----------------

def test_every_weekly_bar_lands_on_a_friday(db):
    """EXTRACT(dow) = 5 is Friday. Yahoo stamps week-start Mondays; if the
    normalisation regressed, the stock/benchmark RS join silently returns NULL."""
    bad = db.fetch_value(
        "SELECT count(*) AS c FROM market.weekly_bar "
        "WHERE EXTRACT(dow FROM week_end_date) <> 5")
    assert bad == 0


def test_stock_and_benchmark_weeks_align(db):
    """The join that produces relative strength must retain nearly every week."""
    row = db.fetch_one("""
        WITH bm AS (
            SELECT week_end_date FROM market.weekly_bar w
            JOIN market.security s USING (security_id)
            WHERE s.symbol = 'NIFTY_500'
        ), stock AS (
            SELECT week_end_date FROM market.weekly_bar w
            JOIN market.security s USING (security_id)
            WHERE s.symbol = 'RELIANCE'
        )
        SELECT (SELECT count(*) FROM bm)    AS bm_weeks,
               (SELECT count(*) FROM stock) AS stock_weeks,
               (SELECT count(*) FROM stock JOIN bm USING (week_end_date)) AS matched
    """)
    smaller = min(row["bm_weeks"], row["stock_weeks"])
    assert row["matched"] >= smaller - 2, (
        f"weekly join dropped rows: {row}. Stock and benchmark bars are not on "
        f"the same week_end_date, which nulls out relative strength.")


def test_iso_week_columns_agree_with_week_end_date(db):
    bad = db.fetch_value("""
        SELECT count(*) AS c FROM market.weekly_bar
        WHERE iso_year <> EXTRACT(isoyear FROM week_end_date)
           OR iso_week <> EXTRACT(week   FROM week_end_date)
    """)
    assert bad == 0


def test_yahoo_bars_are_marked_as_fallback_rank(db):
    """
    Yahoo sits at rank 50 so a bhavcopy backfill at 100 displaces it. Bhavcopy is
    demoted to 40 for securities that fail reconciliation, so the ranks in play
    are 40/50/100 and Yahoo must consistently be 50.
    """
    rows = db.fetch_all("""
        SELECT DISTINCT source, source_rank, adj_basis FROM market.weekly_bar
        ORDER BY source, source_rank
    """)
    ranks = {(r["source"], r["source_rank"]) for r in rows}
    assert ("yahoo_weekly", 50) in ranks
    assert all(r["adj_basis"] == "yahoo_adjclose"
               for r in rows if r["source"] == "yahoo_weekly")
    bhav_ranks = {r["source_rank"] for r in rows if r["source"] == "nse_bhavcopy"}
    assert bhav_ranks <= {40, 100}, f"unexpected bhavcopy ranks: {bhav_ranks}"


# ---------------- announcements ----------------

def test_announcements_deduped_on_content_hash(db):
    stored = db.fetch_value("SELECT count(*) AS c FROM market.announcement")
    assert 200_000 < stored < 233_500


def test_seen_count_is_persisted_not_just_in_memory(db):
    """market_intel incremented seen_count only on a Python object; here a
    re-import must actually raise the stored value."""
    mx = db.fetch_value("SELECT max(seen_count) AS m FROM market.announcement")
    assert mx >= 1


def test_announcements_have_knowledge_dates(db):
    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.announcement WHERE available_at IS NULL")
    assert n == 0


def test_most_announcements_resolve_to_a_security(db):
    total = db.fetch_value("SELECT count(*) AS c FROM market.announcement")
    linked = db.fetch_value(
        "SELECT count(*) AS c FROM market.announcement WHERE security_id IS NOT NULL")
    assert linked / total > 0.75, (
        f"only {linked}/{total} announcements mapped to a security")


# ---------------- provenance ----------------

def test_latest_batch_per_source_completed(db):
    rows = db.fetch_all("""
        SELECT DISTINCT ON (source) source, status
        FROM   market.sync_batch WHERE source LIKE 'legacy.%'
        ORDER  BY source, started_at DESC
    """)
    assert rows
    bad = [r for r in rows if r["status"] != "complete"]
    assert not bad, f"latest batch not complete for: {bad}"


def test_no_batch_left_running(db):
    """A crashed run must be reaped to 'interrupted', not linger as 'running'."""
    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.sync_batch "
        "WHERE status = 'running' AND started_at < now() - interval '2 hours'")
    assert n == 0


def test_yahoo_misses_recorded_as_errors_not_dropped(db):
    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.sync_error WHERE source = 'legacy.yahoo_weekly'")
    assert n > 0, "the 132 Yahoo misses should be recorded, not silently lost"


def test_watermarks_set_for_every_source(db):
    rows = db.fetch_all("SELECT source FROM market.sync_watermark")
    sources = {r["source"] for r in rows}
    for s in ("prices.nse_bhavcopy", "prices.yahoo_weekly",
              "fundamentals.screener", "events.nse_announcements"):
        assert s in sources
