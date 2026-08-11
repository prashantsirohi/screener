"""
Corporate actions, derived from the exchange's own price file.

On an ex-date NSE restates PRVSCLSGPRIC onto the new basis, so
`prev_close[t] / close[t-1]` IS the adjustment factor. That makes bhavcopy a
primary source for splits, bonuses and consolidations - which matters because
the NSE corporate-actions API thins out for older periods.

Two guards make this trustworthy rather than merely clever:

* **Gap safety.** LAG() reaches across holes in the history. A partial backfill
  once produced 1,761 phantom events by comparing a 2023 price to a 2026
  prev_close. Only genuinely consecutive sessions are compared.
* **Cluster rejection.** A real corporate action affects one company. When many
  unrelated securities show the same discontinuity on one date, the cause is a
  missing trading session, not 46 simultaneous splits.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from fractions import Fraction

import pyarrow as pa

from ..analytics.duck import analytics_session, load_sql
from ..config import Settings
from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from .sync_state import close_batch, open_batch, set_watermark

log = logging.getLogger(__name__)

SOURCE = "events.corporate_actions"

# Indian equities are circuit-limited to +/-20% a session, so a move beyond this
# is almost certainly a corporate action rather than trading.
MIN_MOVE = 0.28
MAX_GAP_DAYS = 5         # weekend plus a holiday
SNAP_TOL = 0.05          # how close the factor must sit to a simple ratio
CLUSTER_LIMIT = 8        # more securities than this on one date = missing session

# Price alone cannot separate a shallow bonus from a bad day. A 28% fall gives a
# factor of 0.72, which is within tolerance of a 3:4 bonus (0.75) - and wrongly
# recording that as a corporate action would corrupt the security's entire
# adjusted history.
#
# Below this factor the reading is unambiguous: no circuit-limited session drops
# a stock by 45%. At or above it the event is only accepted when the corporate
# actions feed corroborates it, and is otherwise recorded as 'unconfirmed' and
# excluded from adjustment.
UNAMBIGUOUS_BELOW = 0.55
UNAMBIGUOUS_ABOVE = 1.80


def _snap_ratio(factor, max_denominator: int = 20) -> tuple[float, float] | None:
    """Express the factor as a small ratio, e.g. 0.5 -> 1:2, 0.6667 -> 2:3."""
    factor = float(factor)   # DuckDB hands back Decimal for numeric columns
    try:
        fr = Fraction(factor).limit_denominator(max_denominator)
    except (ValueError, ZeroDivisionError):
        return None
    if fr.numerator <= 0 or fr.denominator <= 0:
        return None
    if abs(float(fr) - factor) > 0.02:
        return None
    return float(fr.numerator), float(fr.denominator)


def _hash(security_id: int, ex_date: date, action_type: str, factor: float) -> str:
    blob = f"{security_id}|{ex_date.isoformat()}|{action_type}|{factor:.6f}"
    return hashlib.sha256(blob.encode()).hexdigest()


def _arrow(con, sql: str, params: dict) -> pa.Table:
    tbl = con.execute(sql, params).arrow()
    return tbl.read_all() if isinstance(tbl, pa.RecordBatchReader) else tbl


def infer(settings: Settings, db: Database, as_of: date, *,
          min_move: float = MIN_MOVE, max_gap_days: int = MAX_GAP_DAYS,
          snap_tol: float = SNAP_TOL, cluster_limit: int = CLUSTER_LIMIT) -> dict:
    bid = open_batch(db, SOURCE, {"as_of": str(as_of), "min_move": min_move,
                                  "max_gap_days": max_gap_days,
                                  "snap_tol": snap_tol})

    with analytics_session(settings) as con:
        tbl = _arrow(con, load_sql("infer_corporate_actions.sql"),
                     {"as_of": as_of, "min_move": min_move,
                      "max_gap_days": max_gap_days, "snap_tol": snap_tol})

    cols = {c: tbl.column(c).to_pylist() for c in tbl.column_names}
    n_raw = tbl.num_rows

    # Reject dates where too many unrelated securities move together: that is a
    # session missing from the history, not a wave of simultaneous splits.
    per_date: dict[date, int] = {}
    for d in cols["ex_date"]:
        per_date[d] = per_date.get(d, 0) + 1
    suspect = {d for d, n in per_date.items() if n > cluster_limit}
    if suspect:
        log.warning("rejecting %d date(s) with clustered discontinuities "
                    "(likely a missing session): %s",
                    len(suspect), sorted(suspect)[:5])

    rows, kept, ambiguous = [], 0, 0
    for i in range(n_raw):
        ex_date = cols["ex_date"][i]
        if ex_date in suspect:
            continue
        # The SNAPPED ratio is the corporate action; the observed factor also
        # carries that day's ordinary price move, which must not be baked into
        # the adjustment or every pre-action price is off by a few percent.
        snap = cols["snap_ratio"][i]
        if not snap or snap <= 0:
            continue
        sid = cols["security_id"][i]
        snap_f = float(snap)
        action_type = "split" if snap_f < 1 else "consolidation"
        unambiguous = snap_f <= UNAMBIGUOUS_BELOW or snap_f >= UNAMBIGUOUS_ABOVE
        confidence = "inferred" if unambiguous else "unconfirmed"
        if not unambiguous:
            ambiguous += 1
        ratio = _snap_ratio(snap)
        rows.append((
            sid, ex_date, action_type,
            ratio[0] if ratio else None,
            ratio[1] if ratio else None,
            None, snap,
            f"inferred from close discontinuity "
            f"({cols['prior_close'][i]} -> {cols['close'][i]}, "
            f"observed {float(cols['factor'][i]):.4f}, snapped {snap_f:.4f})"
            + ("" if unambiguous else " - shallow, needs corroboration"),
            "inferred_close_gap", confidence,
            _hash(sid, ex_date, action_type, snap_f),
        ))
        kept += 1

    if rows:
        cs = ("security_id", "ex_date", "action_type", "ratio_from", "ratio_to",
              "amount_inr", "adjustment_factor", "purpose_text", "source",
              "confidence", "action_hash")
        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "ca_in", {
                "security_id": "bigint", "ex_date": "date", "action_type": "text",
                "ratio_from": "numeric", "ratio_to": "numeric", "amount_inr": "numeric",
                "adjustment_factor": "numeric", "purpose_text": "text",
                "source": "text", "confidence": "text", "action_hash": "text"})
            copy_rows(cur, "ca_in", cs, rows)
            cur.execute(f"""
                INSERT INTO market.corporate_action ({", ".join(cs)})
                SELECT DISTINCT ON (action_hash) {", ".join(cs)}
                FROM   staging.ca_in
                ON CONFLICT (action_hash) DO NOTHING
            """)
            drop_staging(cur, "ca_in")

    stored = db.fetch_value("SELECT count(*) AS c FROM market.corporate_action")
    close_batch(db, bid, status="complete", total=n_raw, ok=kept,
                skipped=n_raw - kept, rows=len(rows))
    set_watermark(db, SOURCE, "inferred", as_of.isoformat(), rows=len(rows))

    return {"candidates": n_raw, "kept": kept,
            "rejected_clustered": n_raw - kept,
            "unconfirmed_shallow": ambiguous,
            "suspect_dates": sorted(str(d) for d in suspect)[:10],
            "stored_total": stored}


def from_api(db: Database, start: date, end: date) -> dict:
    """
    Pull splits and bonuses from the NSE corporate actions feed.

    This is the authoritative source, so its rows carry confidence 'reported' and
    supersede an inferred factor for the same security and date. Inference stays
    useful for periods the feed thins out on, and as a cross-check.
    """
    from ..sources.nse_corporate_actions import CorporateActionCollector

    bid = open_batch(db, SOURCE, {"mode": "api", "start": str(start), "end": str(end)})
    smap = {r["symbol"]: r["security_id"] for r in db.fetch_all(
        "SELECT symbol, security_id FROM market.security WHERE exchange = 'NSE'")}
    alias = {r["symbol"]: r["security_id"] for r in db.fetch_all(
        "SELECT symbol, security_id FROM market.security_alias WHERE exchange = 'NSE'")}

    collector = CorporateActionCollector()
    rows, unmapped, seen = [], 0, 0
    for ca in collector.collect(start, end):
        seen += 1
        sid = smap.get(ca.symbol) or alias.get(ca.symbol)
        if sid is None:
            unmapped += 1
            continue
        rows.append((
            sid, ca.ex_date, ca.action_type, ca.ratio_from, ca.ratio_to,
            None, ca.factor, ca.subject, "nse_api", "reported",
            _hash(sid, ca.ex_date, ca.action_type, ca.factor),
        ))

    if rows:
        cs = ("security_id", "ex_date", "action_type", "ratio_from", "ratio_to",
              "amount_inr", "adjustment_factor", "purpose_text", "source",
              "confidence", "action_hash")
        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "caapi_in", {
                "security_id": "bigint", "ex_date": "date", "action_type": "text",
                "ratio_from": "numeric", "ratio_to": "numeric", "amount_inr": "numeric",
                "adjustment_factor": "numeric", "purpose_text": "text",
                "source": "text", "confidence": "text", "action_hash": "text"})
            copy_rows(cur, "caapi_in", cs, rows)
            cur.execute(f"""
                INSERT INTO market.corporate_action ({", ".join(cs)})
                SELECT DISTINCT ON (action_hash) {", ".join(cs)}
                FROM   staging.caapi_in
                ON CONFLICT (action_hash) DO UPDATE SET
                    source = 'nse_api', confidence = 'reported',
                    purpose_text = EXCLUDED.purpose_text
            """)
            # An authoritative row retires the inferred guess for the same event,
            # otherwise BOTH factors are applied and the history is rescaled
            # twice - SPORTKING's 1:10 split was recorded at 2024-09-09 by the
            # divergence method and 2024-09-13 by the feed, and 0.1 x 0.1 = 0.01
            # was being applied to every prior bar.
            #
            # The window is +/-7 days because the divergence method only localises
            # an event to a week, so it can land several days off the true ex-date.
            cur.execute("""
                DELETE FROM market.corporate_action a
                WHERE  a.source <> 'nse_api'
                  AND  EXISTS (
                       SELECT 1 FROM market.corporate_action b
                       WHERE  b.source = 'nse_api'
                         AND  b.security_id = a.security_id
                         AND  b.ex_date BETWEEN a.ex_date - 7 AND a.ex_date + 7)
            """)
            superseded = cur.rowcount
            drop_staging(cur, "caapi_in")
    else:
        superseded = 0

    stored = db.fetch_value("SELECT count(*) AS c FROM market.corporate_action")
    close_batch(db, bid, status="complete", total=seen, ok=len(rows),
                skipped=unmapped, rows=len(rows))
    set_watermark(db, SOURCE, "api", end.isoformat(), rows=len(rows))
    return {"feed_actions": seen, "mapped": len(rows), "unmapped_symbols": unmapped,
            "superseded_inferred": superseded, "stored_total": stored}


def from_divergence(settings: Settings, db: Database, as_of: date, *,
                    min_step: float = 0.10, snap_tol: float = 0.04) -> dict:
    """
    Recover actions that the bhavcopy-side inference missed, using Yahoo's
    adjusted series as an independent second opinion.

    Recorded with confidence 'corroborated': two providers disagreeing at a clean
    ratio on one week is stronger evidence than a single-source price gap, so
    unlike 'unconfirmed' these ARE applied to the adjusted series.

    The ex-date is resolved to the exact session inside the week, since the
    weekly comparison only localises it to a week.
    """
    bid = open_batch(db, SOURCE, {"as_of": str(as_of), "mode": "divergence",
                                  "min_step": min_step})
    with analytics_session(settings) as con:
        tbl = _arrow(con, load_sql("actions_from_divergence.sql"),
                     {"as_of": as_of, "min_step": min_step, "snap_tol": snap_tol})

    cols = {c: tbl.column(c).to_pylist() for c in tbl.column_names}
    rows = []
    for i in range(tbl.num_rows):
        sid = cols["security_id"][i]
        snap = float(cols["snap_ratio"][i])
        if snap <= 0:
            continue
        # The week is known; pick the first session of that week as the ex-date.
        ex = db.fetch_value("""
            SELECT min(trade_date) AS d FROM market.price_daily
            WHERE security_id = %s
              AND trade_date > %s AND trade_date <= %s
        """, (sid, cols["prev_week"][i], cols["ex_week"][i]))
        if ex is None:
            continue
        action_type = cols["action_type"][i]
        ratio = _snap_ratio(snap)
        rows.append((
            sid, ex, action_type,
            ratio[0] if ratio else None, ratio[1] if ratio else None,
            None, snap,
            f"recovered from bhavcopy/Yahoo divergence "
            f"(step {float(cols['step'][i]):.4f} snapped {snap:.4f})",
            "yahoo_divergence", "corroborated",
            _hash(sid, ex, action_type, snap),
        ))

    if rows:
        cs = ("security_id", "ex_date", "action_type", "ratio_from", "ratio_to",
              "amount_inr", "adjustment_factor", "purpose_text", "source",
              "confidence", "action_hash")
        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "cad_in", {
                "security_id": "bigint", "ex_date": "date", "action_type": "text",
                "ratio_from": "numeric", "ratio_to": "numeric", "amount_inr": "numeric",
                "adjustment_factor": "numeric", "purpose_text": "text",
                "source": "text", "confidence": "text", "action_hash": "text"})
            copy_rows(cur, "cad_in", cs, rows)
            cur.execute(f"""
                INSERT INTO market.corporate_action ({", ".join(cs)})
                SELECT DISTINCT ON (action_hash) {", ".join(cs)}
                FROM   staging.cad_in
                ON CONFLICT (action_hash) DO NOTHING
            """)
            drop_staging(cur, "cad_in")

    stored = db.fetch_value("SELECT count(*) AS c FROM market.corporate_action")
    close_batch(db, bid, status="complete", total=tbl.num_rows, ok=len(rows),
                rows=len(rows))
    return {"divergence_candidates": tbl.num_rows, "recovered": len(rows),
            "stored_total": stored}
