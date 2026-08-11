"""
Share-count sync from NSE quote-equity.

Solves the largest data hole in the legacy screen: 307 companies had no market
cap because screener.in served blank pages, and the fallback of
`equity capital / face value` read its input from that same blank page, so it
recovered exactly none of them.

`securityInfo.issuedSize` comes from the exchange, so market cap becomes
`issued_size x bhavcopy close` and no longer depends on the aggregator at all.

Share counts move only on corporate actions, so the refresh cadence is slow and
the default run only fetches securities that lack a recent figure.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from ..http.errors import HttpError
from ..sources.nse_quote import QuoteCollector
from .sync_state import (close_batch, mark_stale_batches, open_batch,
                         record_error, set_watermark)

log = logging.getLogger(__name__)

SOURCE = "reference.nse_quote"
METRIC = "shares_outstanding"
DEFAULT_MAX_AGE_DAYS = 90


def _targets(db: Database, only_missing: bool, max_age_days: int,
             limit: int | None) -> list[dict]:
    """
    ACTIVE series-EQ securities needing a share count, rarest data first.

    is_active matters: the three-year backfill registered 427 delisted or
    renamed symbols, and without this filter the collector chases 2,513 targets
    to serve a 2,086-name screening universe - a fifth of the request budget
    spent on companies that cannot be screened.
    """
    sql = """
        SELECT s.security_id, s.symbol
        FROM   market.security s
        LEFT JOIN LATERAL (
            SELECT cm.as_of_date
            FROM   market.company_metric cm
            WHERE  cm.security_id = s.security_id AND cm.metric = %(metric)s
            ORDER  BY cm.as_of_date DESC
            LIMIT  1
        ) last ON true
        WHERE  s.series = 'EQ' AND s.exchange = 'NSE' AND s.security_type = 'equity'
          AND  s.is_active
    """
    params: dict = {"metric": METRIC}
    if only_missing:
        sql += " AND (last.as_of_date IS NULL OR last.as_of_date < %(cutoff)s)"
        params["cutoff"] = date.today() - timedelta(days=max_age_days)
    sql += " ORDER BY (last.as_of_date IS NOT NULL), s.symbol"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.fetch_all(sql, params)


def sync(db: Database, *, limit: int | None = None, only_missing: bool = True,
         max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict:
    mark_stale_batches(db)
    targets = _targets(db, only_missing, max_age_days, limit)
    if not targets:
        return {"status": "current", "fetched": 0, "targets": 0}

    bid = open_batch(db, SOURCE, {"targets": len(targets), "only_missing": only_missing})
    collector = QuoteCollector()
    today = date.today()

    metric_rows: list[tuple] = []
    enrich: list[tuple] = []
    ok = failed = no_size = 0

    for i, t in enumerate(targets, 1):
        sym = t["symbol"]
        try:
            q = collector.fetch(sym)
        except (HttpError, Exception) as exc:         # noqa: BLE001 - isolate the symbol
            record_error(db, bid, SOURCE, sym, str(exc), error_class=type(exc).__name__)
            # Persist the failure so it is retried on a later run instead of
            # being lost with the process. NSE's quote endpoint answers 403 to
            # unauthenticated clients regardless of pacing, so this queue is what
            # keeps the gap visible and actionable rather than silently empty.
            db.execute("""
                INSERT INTO market.fetch_retry_queue
                    (source, scope, reason, attempts, state, next_attempt_at, last_error)
                VALUES (%s, %s, 'http_error', 1, 'pending',
                        now() + interval '6 hours', %s)
                ON CONFLICT (source, scope) DO UPDATE SET
                    attempts = market.fetch_retry_queue.attempts + 1,
                    state = 'pending',
                    next_attempt_at = now() + interval '6 hours',
                    last_error = EXCLUDED.last_error,
                    updated_at = now()
            """, (SOURCE, sym, str(exc)[:2000]))
            failed += 1
            continue

        if q.issued_size:
            metric_rows.append((t["security_id"], today, METRIC, float(q.issued_size),
                                None, "nse_quote.issuedSize"))
            ok += 1
        else:
            no_size += 1

        enrich.append((t["security_id"], q.isin, q.industry, q.face_value))

        if i % 100 == 0:
            log.info("quote sync %d/%d (ok=%d failed=%d)", i, len(targets), ok, failed)

    if metric_rows:
        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "cm_in", {
                "security_id": "bigint", "as_of_date": "date", "metric": "text",
                "value_num": "numeric", "value_text": "text", "computed_by": "text"})
            copy_rows(cur, "cm_in",
                      ("security_id", "as_of_date", "metric", "value_num",
                       "value_text", "computed_by"), metric_rows)
            cur.execute("""
                INSERT INTO market.company_metric
                    (security_id, as_of_date, metric, value_num, value_text, computed_by)
                SELECT DISTINCT ON (security_id, as_of_date, metric)
                       security_id, as_of_date, metric, value_num, value_text, computed_by
                FROM   staging.cm_in
                ON CONFLICT (security_id, as_of_date, metric) DO UPDATE SET
                    value_num = EXCLUDED.value_num, computed_at = now()
            """)
            drop_staging(cur, "cm_in")

    # Fill sector/ISIN gaps for symbols outside the index constituent files.
    # COALESCE keeps existing values authoritative; this only fills nulls.
    if enrich:
        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "enrich_in", {
                "security_id": "bigint", "isin": "text", "industry": "text",
                "face_value": "numeric"})
            copy_rows(cur, "enrich_in",
                      ("security_id", "isin", "industry", "face_value"), enrich)
            cur.execute("""
                UPDATE market.security s
                SET    nse_industry = COALESCE(s.nse_industry, e.industry),
                       face_value   = COALESCE(s.face_value, e.face_value),
                       last_seen_at = now()
                FROM   staging.enrich_in e
                WHERE  e.security_id = s.security_id
            """)
            drop_staging(cur, "enrich_in")

    status = "complete" if failed == 0 else "partial"
    close_batch(db, bid, status=status, total=len(targets), ok=ok, failed=failed,
                skipped=no_size, rows=len(metric_rows))
    set_watermark(db, SOURCE, "*", today.isoformat(), status=status,
                  rows=len(metric_rows))

    # Coverage is measured against the SCREENING universe - active series-EQ -
    # not against every security ever registered. Otherwise the delisted names
    # the price backfill added make the exit check unreachable by construction.
    covered = db.fetch_value("""
        SELECT count(DISTINCT cm.security_id) AS c
        FROM   market.company_metric cm
        JOIN   market.security s USING (security_id)
        WHERE  cm.metric = %s AND s.series = 'EQ' AND s.security_type = 'equity'
          AND  s.is_active
    """, (METRIC,))
    active_eq = db.fetch_value(
        "SELECT count(*) AS c FROM market.security "
        "WHERE series = 'EQ' AND security_type = 'equity' AND is_active")
    queued = db.fetch_value(
        "SELECT count(*) AS c FROM market.fetch_retry_queue "
        "WHERE source = %s AND state = 'pending'", (SOURCE,))

    return {"status": status, "targets": len(targets), "fetched": ok,
            "no_issued_size": no_size, "failed": failed, "queued_for_retry": queued,
            "coverage_active_eq": f"{covered}/{active_eq}"}
