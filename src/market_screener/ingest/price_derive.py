"""
Derive the adjusted daily series and weekly bars.

Both are pure functions of price_daily + corporate_action, so both tables are
safely rebuildable. The compute happens in DuckDB; results come back as Arrow
and are written to Postgres by psycopg. DuckDB never writes to Postgres - rule 1
of the two-engine contract, so there is only ever one writer.
"""

from __future__ import annotations

import logging
from datetime import date

import pyarrow as pa

from ..analytics.duck import analytics_session, load_sql
from ..config import Settings
from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from .sync_state import close_batch, open_batch, set_watermark

log = logging.getLogger(__name__)

ADJ_COLS = ("security_id", "trade_date", "adj_open", "adj_high", "adj_low",
            "adj_close", "adj_volume", "cum_adj_factor", "adj_basis")
ADJ_STAGING = {
    "security_id": "bigint", "trade_date": "date", "adj_open": "numeric",
    "adj_high": "numeric", "adj_low": "numeric", "adj_close": "numeric",
    "adj_volume": "bigint", "cum_adj_factor": "numeric", "adj_basis": "text",
}

WEEK_COLS = ("security_id", "week_end_date", "iso_year", "iso_week", "open",
             "high", "low", "close", "volume", "turnover_inr", "day_count",
             "source", "source_rank", "adj_basis", "is_complete", "last_trade_date")
WEEK_STAGING = {
    "security_id": "bigint", "week_end_date": "date", "iso_year": "smallint",
    "iso_week": "smallint", "open": "numeric", "high": "numeric", "low": "numeric",
    "close": "numeric", "volume": "bigint", "turnover_inr": "numeric",
    "day_count": "smallint", "source": "text", "source_rank": "smallint",
    "adj_basis": "text", "is_complete": "boolean", "last_trade_date": "date",
}


def _arrow(con, sql: str, params: dict | None = None) -> pa.Table:
    """DuckDB >=1.3 returns a RecordBatchReader from .arrow(); normalise to a Table."""
    res = con.execute(sql, params or {})
    tbl = res.arrow()
    return tbl.read_all() if isinstance(tbl, pa.RecordBatchReader) else tbl


# Price ratios that real Indian corporate actions produce.
#
# Splits are face-value ratios (10->1, 10->2, 10->5, 5->1, 2->1). Bonuses of a:b
# give b/(a+b), so 1:1 -> 0.5, 1:2 -> 0.667, 2:1 -> 0.333, and so on.
#
# The list is explicit rather than derived from Fraction.limit_denominator(),
# which finds a "clean" fraction for almost any number - 1.17 becomes 7/6 - and
# therefore classified ordinary dividend divergence as a missing split.
# Splits and bonuses always REDUCE the price, so their ratios are below 1.
_SPLIT_AND_BONUS_RATIOS = (
    0.05, 0.1, 0.125, 0.1667, 0.2, 0.25, 0.3333, 0.4, 0.5,
    0.6, 0.625, 0.6667, 0.75, 0.8, 0.8333,
)
# Consolidations raise it, and only at whole-number ratios: 2:1, 5:1, 10:1. The
# inverses of the bonus ratios (1.2, 1.25, 1.333, 1.5) are NOT corporate actions
# and must not be listed - they blanket the 1.1-1.5 band where dividend
# divergence lives, and including them matched 24 of 34 arbitrary values.
_CONSOLIDATION_RATIOS = (2.0, 2.5, 3.0, 4.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)

_ACTION_RATIOS = tuple(sorted(_SPLIT_AND_BONUS_RATIOS + _CONSOLIDATION_RATIOS))
_RATIO_TOLERANCE = 0.03      # 3% - tight enough to exclude a 15% dividend


def _is_action_ratio(step: float, min_move: float = 0.10) -> bool:
    """Does this step land on a ratio a split, bonus or consolidation produces?"""
    if not step or step <= 0 or abs(step - 1.0) < min_move:
        return False
    return any(abs(step - r) <= _RATIO_TOLERANCE * r for r in _ACTION_RATIOS)


def _rows(table: pa.Table, columns) -> list[tuple]:
    cols = [table.column(c).to_pylist() for c in columns]
    return list(zip(*cols))


def rebuild_adjusted(settings: Settings, db: Database, as_of: date) -> dict:
    """price_daily + corporate_action -> price_daily_adj."""
    bid = open_batch(db, "derive.price_daily_adj", {"as_of": str(as_of)})
    with analytics_session(settings) as con:
        tbl = _arrow(con, load_sql("adjust.sql"), {"as_of": as_of})

    rows = _rows(tbl, ADJ_COLS)
    if not rows:
        close_batch(db, bid, status="complete", rows=0, note="no daily bars")
        return {"rows": 0}

    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "adj_in", ADJ_STAGING)
        copy_rows(cur, "adj_in", ADJ_COLS, rows)
        cur.execute(f"""
            INSERT INTO market.price_daily_adj ({", ".join(ADJ_COLS)})
            SELECT {", ".join(ADJ_COLS)} FROM staging.adj_in
            ON CONFLICT (security_id, trade_date) DO UPDATE SET
                adj_open = EXCLUDED.adj_open, adj_high = EXCLUDED.adj_high,
                adj_low = EXCLUDED.adj_low, adj_close = EXCLUDED.adj_close,
                adj_volume = EXCLUDED.adj_volume,
                cum_adj_factor = EXCLUDED.cum_adj_factor,
                adj_basis = EXCLUDED.adj_basis, computed_at = now()
        """)
        drop_staging(cur, "adj_in")

    adjusted = db.fetch_value(
        "SELECT count(*) AS c FROM market.price_daily_adj WHERE cum_adj_factor <> 1.0")
    close_batch(db, bid, status="complete", total=len(rows), ok=len(rows), rows=len(rows))
    return {"rows": len(rows), "bars_with_adjustment": adjusted}


def rebuild_weekly(settings: Settings, db: Database, as_of: date,
                   min_days: int = 1) -> dict:
    """price_daily_adj -> weekly_bar at source_rank 100.

    The upsert only overwrites when the incoming rank is at least as high, so
    bhavcopy-derived bars displace imported Yahoo bars (rank 50) per security as
    the backfill reaches them, without a coordinated cutover.
    """
    bid = open_batch(db, "derive.weekly_bar", {"as_of": str(as_of)})
    with analytics_session(settings) as con:
        tbl = _arrow(con, load_sql("weekly_bars.sql"),
                     {"as_of": as_of, "min_days": min_days})

    rows = _rows(tbl, WEEK_COLS)
    if not rows:
        close_batch(db, bid, status="complete", rows=0, note="no adjusted bars")
        return {"rows": 0}

    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "week_in", WEEK_STAGING)
        copy_rows(cur, "week_in", WEEK_COLS, rows)
        cur.execute(f"""
            INSERT INTO market.weekly_bar ({", ".join(WEEK_COLS)})
            SELECT {", ".join(WEEK_COLS)} FROM staging.week_in
            ON CONFLICT (security_id, week_end_date, source) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume,
                turnover_inr = EXCLUDED.turnover_inr, day_count = EXCLUDED.day_count,
                source_rank = EXCLUDED.source_rank,
                adj_basis = EXCLUDED.adj_basis,
                is_complete = EXCLUDED.is_complete,
                last_trade_date = EXCLUDED.last_trade_date,
                computed_at = now()
        """)
        drop_staging(cur, "week_in")

    by_source = db.fetch_all(
        "SELECT source, count(*) AS n, count(DISTINCT security_id) AS securities "
        "FROM market.weekly_bar GROUP BY source ORDER BY source")
    close_batch(db, bid, status="complete", total=len(rows), ok=len(rows), rows=len(rows))
    set_watermark(db, "derive.weekly_bar", "*", as_of.isoformat(), rows=len(rows))
    return {"rows": len(rows),
            "by_source": {r["source"]: {"bars": r["n"], "securities": r["securities"]}
                          for r in by_source}}


def reconcile_sources(settings: Settings, db: Database, as_of: date) -> dict:
    """
    Compare bhavcopy-derived and Yahoo weekly closes where both exist.

    Any security that never reaches 'agree' should not be silently flipped to the
    new basis - the divergence is either a missed corporate action or the
    total-return vs price-return difference, and the two need different fixes.
    """
    with analytics_session(settings) as con:
        # Reads src_weekly_bar_all, which retains every source's bars; the
        # resolved view would only ever show the winner and there would be
        # nothing to compare. ("both" is a DuckDB reserved word - hence "paired".)
        #
        # What this can and cannot test. Yahoo's adjclose is TOTAL return and the
        # bhavcopy series is PRICE return, so on a dividend payer the two diverge
        # steadily as you go back - measured here at ~1.3% over three years. That
        # is expected and is NOT an error, so a blanket "95% of all weeks within
        # 1%" test would fail every dividend payer in the market.
        #
        # Two things are worth testing:
        #   * RECENT agreement. Both bases coincide at the latest bar, so the last
        #     quarter must match tightly. It does: 1,954 of 1,954 within 0.1%.
        #   * A STEP in the ratio. Dividends produce gradual drift; a missed split
        #     produces a sudden jump. That step is the real defect signal.
        tbl = _arrow(con, """
            WITH paired AS (
                SELECT w.security_id, w.week_end_date,
                       w.close / y.close AS ratio
                FROM   src_weekly_bar_all w
                JOIN   src_weekly_bar_all y
                       ON y.security_id   = w.security_id
                      AND y.week_end_date = w.week_end_date
                WHERE  w.source = 'nse_bhavcopy'
                  AND  y.source = 'yahoo_weekly'
                  AND  y.close > 0 AND w.close > 0
            ),
            stepped AS (
                SELECT security_id, week_end_date, ratio,
                       ratio / NULLIF(LAG(ratio) OVER (PARTITION BY security_id
                                                       ORDER BY week_end_date), 0)
                           AS step
                FROM paired
            ),
            ranked AS (
                -- Rank by WEEK, never by the ratio itself. Joining on a floating
                -- point ratio is a many-to-many explosion: equal ratios in
                -- different weeks all match each other, and 1,757 of 1,954
                -- securities reported a bogus weeks_compared - one 158-week
                -- series claimed 23,720 comparisons.
                SELECT security_id, week_end_date, ratio,
                       ROW_NUMBER() OVER (PARTITION BY security_id
                                          ORDER BY week_end_date DESC) AS rn
                FROM paired
            ),
            biggest AS (
                -- The single largest step per security, and its signed ratio, so
                -- the caller can test whether it lands on a clean split ratio.
                SELECT DISTINCT ON (security_id) security_id, step
                FROM   stepped WHERE step IS NOT NULL
                ORDER  BY security_id, ABS(step - 1) DESC
            )
            SELECT r.security_id,
                   COUNT(*)                                              AS weeks_compared,
                   SUM(CASE WHEN r.rn <= 13 AND ABS(r.ratio - 1) < 0.01
                            THEN 1 ELSE 0 END)                           AS weeks_matching,
                   MAX(ABS(s.step - 1)) * 100                            AS max_step_pct,
                   MEDIAN(ABS(r.ratio - 1)) * 100                        AS median_diff_pct,
                   SUM(CASE WHEN r.rn <= 13 THEN 1 ELSE 0 END)           AS recent_weeks,
                   MAX(b.step)                                           AS biggest_step
            FROM ranked r
            LEFT JOIN stepped s
                   ON s.security_id = r.security_id
                  AND s.week_end_date = r.week_end_date
            LEFT JOIN biggest b ON b.security_id = r.security_id
            GROUP BY r.security_id
        """)

    rows = []
    for sid, cmp_, match, step, med, recent, biggest in zip(
            tbl.column("security_id").to_pylist(),
            tbl.column("weeks_compared").to_pylist(),
            tbl.column("weeks_matching").to_pylist(),
            tbl.column("max_step_pct").to_pylist(),
            tbl.column("median_diff_pct").to_pylist(),
            tbl.column("recent_weeks").to_pylist(),
            tbl.column("biggest_step").to_pylist()):
        step = float(step or 0.0)
        recent = max(int(recent or 0), 1)
        recent_ok = (match or 0) / recent

        # A step alone is not evidence of a missing split. A special dividend or
        # a rights issue moves Yahoo's total-return series and not the
        # price-return one, and does so by an untidy amount: measured across the
        # universe, 517 of 566 large steps imply no clean ratio at all. Only a
        # step that lands on a ratio a real action produces counts.
        snaps_clean = _is_action_ratio(float(biggest)) if biggest else False

        if cmp_ < 8:
            verdict = "insufficient"
        elif step >= 10.0 and snaps_clean:
            verdict = "missed_action"
        elif recent_ok >= 0.9:
            verdict = "agree"
        elif recent_ok >= 0.6 or step >= 10.0:
            # Diverges, but not at a ratio any corporate action would produce.
            verdict = "drift"
        else:
            verdict = "disagree"
        rows.append((sid, as_of, cmp_, match, step, med, verdict, None))

    if rows:
        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "recon_in", {
                "security_id": "bigint", "as_of_date": "date", "weeks_compared": "integer",
                "weeks_matching": "integer", "max_step_pct": "numeric",
                "median_diff_pct": "numeric", "verdict": "text", "note": "text"})
            copy_rows(cur, "recon_in",
                      ("security_id", "as_of_date", "weeks_compared", "weeks_matching",
                       "max_step_pct", "median_diff_pct", "verdict", "note"), rows)
            cur.execute("""
                INSERT INTO market.price_source_reconciliation
                    (security_id, as_of_date, weeks_compared, weeks_matching,
                     max_step_pct, median_diff_pct, verdict, note)
                SELECT security_id, as_of_date, weeks_compared, weeks_matching,
                       max_step_pct, median_diff_pct, verdict, note
                FROM   staging.recon_in
                ON CONFLICT (security_id, as_of_date) DO UPDATE SET
                    weeks_compared = EXCLUDED.weeks_compared,
                    weeks_matching = EXCLUDED.weeks_matching,
                    max_step_pct = EXCLUDED.max_step_pct,
                    median_diff_pct = EXCLUDED.median_diff_pct,
                    verdict = EXCLUDED.verdict, computed_at = now()
            """)
            drop_staging(cur, "recon_in")

    counts = db.fetch_all(
        "SELECT verdict, count(*) AS n FROM market.price_source_reconciliation "
        "WHERE as_of_date = %s GROUP BY verdict ORDER BY verdict", (as_of,))
    return {"compared": len(rows), "verdicts": {r["verdict"]: r["n"] for r in counts}}


def report_source_choice(db: Database) -> dict:
    """
    Report which source each security resolves to, and why.

    Nothing is mutated. `weekly_bar_source_choice` derives the election from bar
    coverage and the latest reconciliation verdict on every read, so a security
    whose bhavcopy history fails to reconcile falls back to Yahoo automatically -
    and a reload of weekly_bar cannot silently undo that, which is exactly what
    happened when the demotion was written into source_rank.
    """
    resolved = db.fetch_all("""
        SELECT source, count(*) AS bars, count(DISTINCT security_id) AS securities
        FROM   market.weekly_bar_resolved GROUP BY source ORDER BY source
    """)
    # Count where reconciliation CHANGED the outcome. The choice view only holds
    # the winning row per security, so a demoted loser never appears in it -
    # looking for effective_rank < source_rank there is structurally always zero.
    by_verdict = db.fetch_all("""
        SELECT r.verdict, c.source, count(*) AS securities
        FROM   market.price_source_reconciliation r
        JOIN   market.weekly_bar_source_choice c USING (security_id)
        WHERE  r.as_of_date = (SELECT max(as_of_date)
                               FROM market.price_source_reconciliation)
        GROUP  BY 1, 2 ORDER BY 1, 2
    """)
    overridden = sum(r["securities"] for r in by_verdict
                     if r["verdict"] in ("missed_action", "disagree")
                     and r["source"] != "nse_bhavcopy")
    return {"securities_overridden_by_reconciliation": overridden,
            "election_by_verdict": {f"{r['verdict']}->{r['source']}": r["securities"]
                                    for r in by_verdict},
            "resolved_by_source": {r["source"]: {"bars": r["bars"],
                                                 "securities": r["securities"]}
                                   for r in resolved}}
