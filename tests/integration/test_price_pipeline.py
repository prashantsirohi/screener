"""
Invariants of the price pipeline.

Several of these encode bugs that actually shipped during the build and were
caught by data inspection rather than by a crash. They exist so the same silent
failure cannot return.
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
    if (d.fetch_value("SELECT count(*) AS c FROM market.price_daily") or 0) < 100_000:
        pytest.skip("price history not backfilled")
    return d


# ---------------- coverage ----------------

def test_three_years_of_sessions_loaded(db):
    n = db.fetch_value("SELECT count(DISTINCT trade_date) AS c FROM market.price_daily")
    assert n >= 700, f"only {n} sessions; expected ~750 over three years"


def test_every_bar_has_turnover(db):
    missing = db.fetch_value(
        "SELECT count(*) AS c FROM market.price_daily WHERE turnover_inr IS NULL")
    assert missing == 0


def test_weekend_trading_sessions_were_captured(db):
    """
    NSE runs occasional Saturday/Sunday sessions - Diwali Muhurat, Budget days,
    disaster-recovery tests. A weekday-only date walker misses them, holing the
    weekly bars and making the split inference read the gap as a corporate action.
    """
    rows = db.fetch_all("""
        SELECT DISTINCT trade_date FROM market.price_daily
        WHERE EXTRACT(dow FROM trade_date) IN (0, 6) ORDER BY trade_date
    """)
    assert len(rows) >= 5, (
        f"expected several weekend sessions in three years, found {len(rows)}")


def test_calendar_marks_both_trading_and_non_trading_days(db):
    trading = db.fetch_value(
        "SELECT count(*) AS c FROM market.trading_calendar WHERE is_trading")
    holidays = db.fetch_value(
        "SELECT count(*) AS c FROM market.trading_calendar WHERE NOT is_trading")
    assert trading > 700 and holidays > 200


# ---------------- corporate actions ----------------

def test_actions_come_predominantly_from_the_authoritative_feed(db):
    rows = db.fetch_all(
        "SELECT source, count(*) AS n FROM market.corporate_action GROUP BY source")
    by_source = {r["source"]: r["n"] for r in rows}
    assert by_source.get("nse_api", 0) > 250, (
        f"the NSE feed should dominate; got {by_source}")


def test_no_double_application_within_a_week(db):
    """
    SPORTKING's 1:10 split was recorded twice - 2024-09-09 by the divergence
    method and 2024-09-13 by the feed - and 0.1 x 0.1 = 0.01 was applied to every
    prior bar. The adjustment must never apply two factors for one event.
    """
    dupes = db.fetch_all("""
        SELECT a.security_id, a.ex_date, b.ex_date AS other_ex_date
        FROM   market.corporate_action a
        JOIN   market.corporate_action b
               ON b.security_id = a.security_id
              AND b.ex_date <> a.ex_date
              AND b.ex_date BETWEEN a.ex_date - 7 AND a.ex_date + 7
        WHERE  a.confidence <> 'unconfirmed' AND b.confidence <> 'unconfirmed'
          AND  a.source = 'nse_api' AND b.source <> 'nse_api'
        LIMIT 5
    """)
    assert not dupes, f"authoritative and inferred rows co-exist for one event: {dupes}"


def test_preference_share_bonuses_are_not_treated_as_equity_actions(db):
    """
    "Scheme Of Arrangement - Bonus Ncrps 4:1" is a bonus of preference shares and
    does not change the equity price. Parsing it as a 4:1 equity bonus rescaled
    TVS Motor's entire history by 0.2.
    """
    bad = db.fetch_all("""
        SELECT s.symbol, ca.purpose_text FROM market.corporate_action ca
        JOIN   market.security s USING (security_id)
        WHERE  ca.purpose_text ILIKE '%ncrps%' OR ca.purpose_text ILIKE '%preference%'
           OR  ca.purpose_text ILIKE '%debenture%'
        LIMIT 5
    """)
    assert not bad, f"non-equity instrument actions leaked in: {bad}"


def test_adjustment_factors_are_sane(db):
    bad = db.fetch_value("""
        SELECT count(*) AS c FROM market.corporate_action
        WHERE adjustment_factor IS NULL OR adjustment_factor <= 0
           OR adjustment_factor > 200
    """)
    assert bad == 0


def test_unconfirmed_actions_are_not_applied(db):
    """A shallow gap could be a bad day; it must not silently rescale a history."""
    n_unconfirmed = db.fetch_value(
        "SELECT count(*) AS c FROM market.corporate_action WHERE confidence='unconfirmed'")
    if n_unconfirmed == 0:
        pytest.skip("no unconfirmed actions in this run")

    # Test the actual invariant - that an unconfirmed action creates no factor
    # discontinuity at its ex-date - rather than looking for a factor that
    # merely EQUALS its ratio. With 700+ actions in the store, a security whose
    # confirmed 1:2 split gives it a 0.5 factor will coincidentally match any
    # unconfirmed 0.5 action, and the old form reported 1,489 false positives.
    leaked = db.fetch_all("""
        WITH solo AS (
            -- unconfirmed actions with no trusted action nearby to explain a step
            SELECT ca.security_id, ca.ex_date, ca.adjustment_factor
            FROM   market.corporate_action ca
            WHERE  ca.confidence = 'unconfirmed'
              AND  NOT EXISTS (
                    SELECT 1 FROM market.corporate_action o
                    WHERE  o.security_id = ca.security_id
                      AND  o.confidence <> 'unconfirmed'
                      AND  o.ex_date BETWEEN ca.ex_date - 7 AND ca.ex_date + 7)
        )
        SELECT s.security_id, s.ex_date,
               before.cum_adj_factor AS f_before, on_day.cum_adj_factor AS f_on
        FROM   solo s
        JOIN LATERAL (
            SELECT cum_adj_factor FROM market.price_daily_adj
            WHERE  security_id = s.security_id AND trade_date < s.ex_date
            ORDER  BY trade_date DESC LIMIT 1) before ON true
        JOIN LATERAL (
            SELECT cum_adj_factor FROM market.price_daily_adj
            WHERE  security_id = s.security_id AND trade_date >= s.ex_date
            ORDER  BY trade_date LIMIT 1) on_day ON true
        WHERE  abs(before.cum_adj_factor - on_day.cum_adj_factor) > 1e-9
        LIMIT  5
    """)
    assert not leaked, (
        f"an unconfirmed action changed the adjustment factor: {leaked}")


# ---------------- adjusted series ----------------

def test_latest_bar_is_unadjusted(db):
    """The most recent bar has no future action, so its factor must be exactly 1."""
    row = db.fetch_one("""
        SELECT count(*) AS n, sum(CASE WHEN cum_adj_factor = 1.0 THEN 1 ELSE 0 END) AS ones
        FROM   market.price_daily_adj
        WHERE  trade_date = (SELECT max(trade_date) FROM market.price_daily_adj)
    """)
    assert row["n"] == row["ones"]


def test_adjusted_matches_raw_where_no_action_applies(db):
    mismatched = db.fetch_value("""
        SELECT count(*) AS c
        FROM   market.price_daily_adj a
        JOIN   market.price_daily p USING (security_id, trade_date)
        WHERE  a.cum_adj_factor = 1.0
          AND  abs(a.adj_close - p.close) > 0.0001
    """)
    assert mismatched == 0


# ---------------- weekly bars ----------------

def test_every_weekly_bar_lands_on_a_friday(db):
    bad = db.fetch_value(
        "SELECT count(*) AS c FROM market.weekly_bar "
        "WHERE EXTRACT(dow FROM week_end_date) <> 5")
    assert bad == 0


def test_both_sources_are_retained_for_reconciliation(db):
    rows = db.fetch_all(
        "SELECT source, count(*) AS n FROM market.weekly_bar GROUP BY source")
    by_source = {r["source"]: r["n"] for r in rows}
    assert "nse_bhavcopy" in by_source and "yahoo_weekly" in by_source, (
        "both series must survive; the resolved view picks the winner")


def test_resolved_view_returns_one_bar_per_security_week(db):
    dupes = db.fetch_value("""
        SELECT count(*) AS c FROM (
            SELECT security_id, week_end_date, count(*) AS n
            FROM market.weekly_bar_resolved GROUP BY 1, 2 HAVING count(*) > 1
        ) x
    """)
    assert dupes == 0


def test_recent_weeks_agree_between_the_two_sources(db):
    """
    Both bases coincide at the latest bar - no adjustment applies - so recent
    weeks must match tightly. Historical divergence is expected (Yahoo is total
    return, bhavcopy is price return) and is not tested here.
    """
    row = db.fetch_one("""
        WITH paired AS (
            SELECT w.security_id, abs(w.close / y.close - 1) AS diff
            FROM   market.weekly_bar w
            JOIN   market.weekly_bar y
                   ON y.security_id = w.security_id
                  AND y.week_end_date = w.week_end_date
            WHERE  w.source = 'nse_bhavcopy' AND y.source = 'yahoo_weekly'
              AND  y.close > 0
              -- is_complete matters: without it this picks the current partial
              -- week, where the two sources hold different numbers of sessions
              -- and are not comparable at all.
              AND  w.is_complete AND y.is_complete
              AND  w.week_end_date = (SELECT max(week_end_date) FROM market.weekly_bar
                                      WHERE source = 'nse_bhavcopy' AND is_complete)
        )
        SELECT count(*) AS n, sum(CASE WHEN diff < 0.001 THEN 1 ELSE 0 END) AS close_enough
        FROM paired
    """)
    assert row["n"] > 1000
    assert row["close_enough"] / row["n"] >= 0.99, (
        f"only {row['close_enough']}/{row['n']} agree on the latest week")


def test_no_security_mixes_sources_or_bases(db):
    """
    One source and one return basis per security, for the whole lookback.

    Per-week source election spliced Yahoo total-return and bhavcopy
    price-return bars into a single series for 1,406 of 2,086 active securities,
    across 3,024 transitions. A 30- or 40-week MA spanning that seam averages two
    different quantities, and the step reads as price action that never happened.
    """
    rows = db.fetch_all("""
        SELECT count(*) FILTER (WHERE srcs > 1)  AS mixed_source,
               count(*) FILTER (WHERE bases > 1) AS mixed_basis,
               count(*)                          AS total
        FROM (
            SELECT r.security_id,
                   count(DISTINCT r.source)    AS srcs,
                   count(DISTINCT r.adj_basis) AS bases
            FROM   market.weekly_bar_resolved r
            JOIN   market.security s USING (security_id)
            WHERE  s.is_active AND s.series = 'EQ'
            GROUP  BY r.security_id
        ) x
    """)
    r = rows[0]
    assert r["mixed_source"] == 0, f"{r['mixed_source']} securities mix sources"
    assert r["mixed_basis"] == 0, f"{r['mixed_basis']} securities mix return bases"
    assert r["total"] > 2000


def test_partial_weeks_are_flagged_not_dated_into_the_future(db):
    """
    A Monday as_of produced 4,052 bars stamped the coming Friday, holding two
    sessions but compared as complete.
    """
    bad = db.fetch_value("""
        SELECT count(*) AS c FROM market.weekly_bar
        WHERE is_complete AND week_end_date > (
            SELECT max(trade_date) FROM market.price_daily)
    """)
    assert bad == 0, f"{bad} bars marked complete beyond the last trading day"

    flagged = db.fetch_value(
        "SELECT count(*) AS c FROM market.weekly_bar WHERE NOT is_complete")
    if flagged:
        latest_complete = db.fetch_value(
            "SELECT max(week_end_date) AS m FROM market.weekly_bar WHERE is_complete")
        earliest_partial = db.fetch_value(
            "SELECT min(week_end_date) AS m FROM market.weekly_bar WHERE NOT is_complete")
        assert earliest_partial > latest_complete


def test_reconciliation_counts_match_the_real_overlap(db):
    """
    Guards a many-to-many join. The recent-weeks CTE was joined on the floating
    point RATIO rather than the week, so equal ratios in different weeks matched
    each other: 1,757 of 1,954 securities reported a bogus weeks_compared and one
    158-week series claimed 23,720 comparisons.
    """
    row = db.fetch_one("""
        WITH truth AS (
            SELECT w.security_id, count(*) AS real_overlap
            FROM   market.weekly_bar w
            JOIN   market.weekly_bar y
                   ON y.security_id = w.security_id
                  AND y.week_end_date = w.week_end_date
            WHERE  w.source = 'nse_bhavcopy' AND y.source = 'yahoo_weekly'
              AND  w.is_complete AND y.is_complete
            GROUP  BY 1
        )
        SELECT count(*) AS n,
               count(*) FILTER (WHERE r.weeks_compared <> t.real_overlap) AS mismatched
        FROM   market.price_source_reconciliation r
        JOIN   truth t USING (security_id)
        WHERE  r.as_of_date = (SELECT max(as_of_date)
                               FROM market.price_source_reconciliation)
    """)
    assert row["n"] > 1000
    assert row["mismatched"] == 0, \
        f"{row['mismatched']} securities have an inflated weeks_compared"


def test_securities_with_a_missing_action_fall_back_to_yahoo(db):
    """
    Reconciliation drives source choice, but only on real evidence.

    `missed_action` means a price step landing on a ratio a split or bonus
    produces - the adjustment has demonstrably missed something, so the series is
    not served.
    """
    leaked = db.fetch_value("""
        SELECT count(*) AS c
        FROM   market.weekly_bar_resolved wr
        JOIN   market.price_source_reconciliation r
               ON r.security_id = wr.security_id
        WHERE  r.verdict = 'missed_action'
          AND  wr.source = 'nse_bhavcopy'
          AND  r.as_of_date = (SELECT max(as_of_date)
                               FROM market.price_source_reconciliation)
    """)
    assert leaked == 0


def test_dividend_divergence_does_not_demote_a_good_series(db):
    """
    The counterpart, and the more easily got wrong half.

    `disagree` and `drift` mean the two series diverge with no step any corporate
    action would produce. Measured across the universe that is dividend yield -
    median 1.69% and 1.97% against 0.13% for agreeing names, and not one of the
    115 `disagree` securities shows a step above 10%. Yahoo strips dividends and
    the price-return series does not, so they are *expected* to diverge.
    Demoting on that basis discarded 81 sound exchange-sourced histories.
    """
    demoted = db.fetch_all("""
        SELECT s.symbol, r.verdict
        FROM   market.weekly_bar_source_choice c
        JOIN   market.price_source_reconciliation r ON r.security_id = c.security_id
        JOIN   market.security s ON s.security_id = c.security_id
        WHERE  r.as_of_date = (SELECT max(as_of_date)
                               FROM market.price_source_reconciliation)
          AND  r.verdict IN ('disagree', 'drift')
          AND  c.source <> 'nse_bhavcopy'
        LIMIT  5
    """)
    assert not demoted, (
        f"dividend-driven divergence demoted a sound series: {demoted}")
