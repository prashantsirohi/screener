"""
screener.in fundamentals sync: staleness-gated refresh plus the blank-page
retry drain.

Two jobs:

* **Refresh.** Only fetch a company whose page is older than the staleness
  window or whose next result is due. That keeps steady state near ~25 pages a
  day rather than the 2,086 that provoked the throttle in the first place.
* **Drain.** Work the retry queue for pages that came back as data-free shells.
  A uniform 3s retry recovered 0 of 20 when measured, so the schedule escalates
  over days and each attempt rebuilds the session.

Rows are claimed with `UPDATE ... RETURNING` so two runs cannot fetch the same
symbol, and the queue lives in Postgres so a crash or reboot loses nothing.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from ..domain import metric_map as mm
from ..domain.periods import parse_period_label
from ..http.client import screener_client
from ..http.errors import HttpError, PermanentHttpError
from ..sources.screener_parse import (blank_reason_is_retryable, is_blank_payload,
                                      parse_company_page)
from .sync_state import (close_batch, mark_stale_batches, open_batch,
                         record_error, set_watermark)

log = logging.getLogger(__name__)

SOURCE = "fundamentals.screener"

# Escalating schedule. Each step also rebuilds the session; from attempt 3 the
# user agent rotates, and from attempt 4 the non-consolidated page is tried
# first in case the consolidated view is what is being withheld.
BACKOFF = {
    1: timedelta(minutes=15),
    2: timedelta(hours=1),
    3: timedelta(hours=6),
    4: timedelta(hours=24),
    5: timedelta(hours=72),
}
MAX_ATTEMPTS = 6
STATEMENT_KEYS = ("profit_loss", "balance_sheet", "cash_flow",
                  "ratios", "quarters", "shareholding")


# A claim is a lease, not a lock. If the process holding it dies, nothing else
# would ever return the row to 'pending' and the symbol is stranded forever -
# which directly contradicts the point of putting the queue in Postgres.
CLAIM_LEASE_MINUTES = 30


def reclaim_stale_claims(db: Database, minutes: int = CLAIM_LEASE_MINUTES) -> int:
    """Return abandoned in_flight rows to pending so a crash cannot strand them."""
    n = db.execute("""
        UPDATE market.fetch_retry_queue
        SET    state = 'pending',
               claimed_at = NULL,
               updated_at = now(),
               last_error = COALESCE(last_error, '') || ' [claim expired]'
        WHERE  source = %s
          AND  state = 'in_flight'
          AND  claimed_at < now() - make_interval(mins => %s)
    """, (SOURCE, minutes))
    if n:
        log.warning("reclaimed %d stranded in_flight claim(s)", n)
    return n


def claim(db: Database, limit: int) -> list[dict]:
    """
    Atomically take up to `limit` due rows so concurrent runs cannot overlap.

    Must go through execute_returning: fetch_all would roll the write back on
    close, handing out rows that were never actually marked in_flight.
    """
    return db.execute_returning("""
        UPDATE market.fetch_retry_queue q
        SET    state = 'in_flight', claimed_at = now(), updated_at = now()
        WHERE  q.retry_id IN (
            SELECT retry_id FROM market.fetch_retry_queue
            WHERE  source = %s AND state = 'pending' AND next_attempt_at <= now()
            ORDER  BY next_attempt_at
            LIMIT  %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING q.retry_id, q.scope AS symbol, q.attempts, q.reason
    """, (SOURCE, limit))


def release(db: Database, retry_id: int, *, resolved: bool,
            attempts: int, error: str | None = None) -> None:
    if resolved:
        db.execute(
            "UPDATE market.fetch_retry_queue SET state='resolved', updated_at=now(), "
            "last_error=NULL WHERE retry_id=%s", (retry_id,))
        return
    nxt = attempts + 1
    if nxt >= MAX_ATTEMPTS:
        db.execute(
            "UPDATE market.fetch_retry_queue SET state='exhausted', attempts=%s, "
            "last_error=%s, updated_at=now() WHERE retry_id=%s",
            (nxt, (error or "")[:2000], retry_id))
        return
    delay = BACKOFF.get(nxt, timedelta(hours=72))
    db.execute(
        "UPDATE market.fetch_retry_queue SET state='pending', attempts=%s, "
        "next_attempt_at = now() + %s, last_error=%s, updated_at=now() "
        "WHERE retry_id=%s",
        (nxt, delay, (error or "")[:2000], retry_id))


def _fetch(client, symbol: str, attempt: int) -> tuple[dict | None, str | None]:
    """Return (payload, error). Later attempts try the standalone page first."""
    order = ["consolidated", ""] if attempt < 4 else ["", "consolidated"]
    last_err = None
    for basis in order:
        url = f"https://www.screener.in/company/{symbol}/" + (f"{basis}/" if basis else "")
        try:
            resp = client.get(url)
        except PermanentHttpError as exc:
            last_err = str(exc)
            continue
        except HttpError as exc:
            last_err = str(exc)
            continue
        if "/register/" in resp.url or "/login/" in resp.url:
            last_err = "gated"
            continue
        rec = parse_company_page(resp.text, symbol, resp.url,
                                 "consolidated" if "consolidated" in resp.url else "standalone")
        blank, reason = is_blank_payload(rec)
        if not blank:
            return rec, None
        last_err = f"blank:{reason}"
        if not blank_reason_is_retryable(reason):
            return None, last_err
    return None, last_err


def _store(db: Database, symbol: str, rec: dict, batch: str) -> int:
    sid = db.fetch_value(
        "SELECT security_id FROM market.security WHERE symbol=%s AND exchange='NSE'",
        (symbol,))
    if sid is None:
        return 0

    fetched_at = datetime.now(timezone.utc)
    basis = rec.get("basis") or "consolidated"
    payload = json.dumps(rec, ensure_ascii=False)

    q_periods = set()
    for series in (rec.get("quarters") or {}).values():
        for lbl in series or {}:
            got = parse_period_label(lbl, "quarter")
            if got:
                q_periods.add(got[1])
    q_latest = max(q_periods) if q_periods else None

    metrics, facts = {}, []
    base_for = {"profit_loss": "annual", "balance_sheet": "annual",
                "cash_flow": "annual", "ratios": "annual",
                "quarters": "quarter", "shareholding": "quarter"}
    for stmt, base in base_for.items():
        for label, series in (rec.get(stmt) or {}).items():
            mid = mm.metric_id(stmt, label)
            metrics.setdefault(mid, mm.describe(stmt, label))
            for lbl, value in (series or {}).items():
                if value is None:
                    continue
                if lbl == "TTM":
                    if q_latest:
                        facts.append((sid, "ttm", q_latest, basis, mid, float(value),
                                      fetched_at, "screener", batch))
                    continue
                got = parse_period_label(lbl, base)
                if got:
                    facts.append((sid, got[0], got[1], basis, mid, float(value),
                                  fetched_at, "screener", batch))
    for label, value in (rec.get("top_ratios") or {}).items():
        if value is None:
            continue
        mid = mm.metric_id("top_ratios", label)
        metrics.setdefault(mid, mm.describe("top_ratios", label))
        facts.append((sid, "snapshot", q_latest or date.today(), basis, mid,
                      float(value), fetched_at, "screener", batch))

    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "m_in", {
            "metric_id": "text", "metric_label": "text", "statement": "text",
            "unit": "text", "higher_is_better": "boolean", "description": "text"})
        copy_rows(cur, "m_in", ("metric_id", "metric_label", "statement", "unit",
                                "higher_is_better", "description"),
                  [(d["metric_id"], d["metric_label"], d["statement"], d["unit"],
                    d["higher_is_better"], d["description"]) for d in metrics.values()])
        cur.execute("""
            INSERT INTO market.metric_dim
                (metric_id, metric_label, statement, unit, higher_is_better, description)
            SELECT DISTINCT ON (metric_id) metric_id, metric_label, statement, unit,
                   higher_is_better, description
            FROM staging.m_in ON CONFLICT (metric_id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO market.screener_page_raw
                (security_id, basis, source_url, fetched_at, payload, payload_hash,
                 is_blank, blank_reason, parser_version)
            VALUES (%s,%s,%s,%s,%s, md5(%s), false, NULL, 'v1')
            ON CONFLICT (security_id, basis, fetched_at) DO NOTHING
        """, (sid, basis, rec.get("source_url"), fetched_at, payload, payload))

        create_staging(cur, "f_in", {
            "security_id": "bigint", "period_type": "text", "report_date": "date",
            "statement_basis": "text", "metric_id": "text", "value": "numeric",
            "available_at": "timestamptz", "source": "text", "sync_batch_id": "text"})
        copy_rows(cur, "f_in",
                  ("security_id", "period_type", "report_date", "statement_basis",
                   "metric_id", "value", "available_at", "source", "sync_batch_id"),
                  facts)
        cur.execute("""
            INSERT INTO market.screener_fact
                (security_id, period_type, report_date, statement_basis, metric_id,
                 value, available_at, source, sync_batch_id)
            SELECT security_id, period_type, report_date, statement_basis, metric_id,
                   value, available_at, source, sync_batch_id
            FROM staging.f_in ON CONFLICT DO NOTHING
        """)
        drop_staging(cur, "m_in", "f_in")
    return len(facts)


def stale_targets(db: Database, *, max_age_days: int, limit: int | None,
                  as_of: date | None = None) -> list[dict]:
    """
    Companies whose fundamentals are due a refresh.

    Two triggers, deliberately narrow. Refreshing all 2,086 pages on a schedule
    is what provoked the throttle that emptied 307 of them in the first place;
    the point of the gate is to keep steady state near ~25 pages a day.

      * age - the cached page is older than the staleness window
      * expected quarter - the last quarterly period on file is old enough that
        the next results should have been declared

    Ordered oldest-first so a capped run always makes progress on the worst.
    """
    as_of = as_of or date.today()
    return db.fetch_all("""
        SELECT s.security_id, s.symbol,
               p.fetched_at::date AS last_fetched,
               q.last_quarter
        FROM   market.security s
        LEFT JOIN LATERAL (
            SELECT fetched_at FROM market.screener_page_raw
            WHERE  security_id = s.security_id AND NOT is_blank
            ORDER  BY fetched_at DESC LIMIT 1
        ) p ON true
        LEFT JOIN LATERAL (
            SELECT max(report_date) AS last_quarter FROM market.screener_fact
            WHERE  security_id = s.security_id AND period_type = 'quarter'
        ) q ON true
        WHERE  s.is_active AND s.series = 'EQ' AND s.security_type = 'equity'
          AND (
                p.fetched_at IS NULL
             OR p.fetched_at::date < %(cutoff)s
             OR (q.last_quarter IS NOT NULL
                 AND q.last_quarter < %(quarter_cutoff)s)
          )
          AND NOT EXISTS (
                SELECT 1 FROM market.fetch_retry_queue r
                WHERE  r.source = %(source)s AND r.scope = s.symbol
                  AND  r.state IN ('pending', 'in_flight'))
        ORDER  BY p.fetched_at NULLS FIRST, s.symbol
        LIMIT  %(limit)s
    """, {"cutoff": as_of - timedelta(days=max_age_days),
          # A quarter's results are normally out within ~45 days of period end,
          # so a last-quarter older than ~135 days means we are behind.
          "quarter_cutoff": as_of - timedelta(days=135),
          "source": SOURCE, "limit": limit or 100})


def refresh_stale(settings: Settings, db: Database, *, limit: int = 25,
                  max_age_days: int | None = None, sleep_sec: float = 3.0,
                  as_of: date | None = None) -> dict:
    """Fetch and store the companies the staleness gate selects."""
    mark_stale_batches(db)
    reclaim_stale_claims(db)
    max_age_days = max_age_days or settings.screen.fundamentals_max_age_days

    targets = stale_targets(db, max_age_days=max_age_days, limit=limit, as_of=as_of)
    if not targets:
        return {"targets": 0, "note": "everything within the staleness window"}

    bid = open_batch(db, SOURCE, {"mode": "refresh", "targets": len(targets),
                                  "max_age_days": max_age_days})
    client = screener_client(settings.screener_session_cookie,
                             min_request_gap_sec=max(sleep_sec, 2.0))
    client.warmup()

    ok = blank = failed = 0
    facts = 0
    for i, t in enumerate(targets, 1):
        rec, err = _fetch(client, t["symbol"], attempt=1)
        if rec is not None:
            facts += _store(db, t["symbol"], rec, bid)
            ok += 1
        elif err and err.startswith("blank"):
            # Quarantine and queue rather than dropping the company.
            blank += 1
            db.execute("""
                INSERT INTO market.fetch_retry_queue
                    (source, scope, reason, attempts, state, next_attempt_at, last_error)
                VALUES (%s, %s, 'blank_page', 1, 'pending',
                        now() + interval '15 minutes', %s)
                ON CONFLICT (source, scope) DO UPDATE SET
                    state = 'pending', attempts = market.fetch_retry_queue.attempts + 1,
                    next_attempt_at = now() + interval '15 minutes',
                    last_error = EXCLUDED.last_error, updated_at = now()
            """, (SOURCE, t["symbol"], err))
        else:
            failed += 1
            record_error(db, bid, SOURCE, t["symbol"], err or "unknown")
        if i % 25 == 0:
            log.info("refresh %d/%d (ok=%d blank=%d failed=%d)",
                     i, len(targets), ok, blank, failed)
        time.sleep(sleep_sec + random.uniform(0, 1.5))

    remaining = db.fetch_value("""
        SELECT count(*) AS c FROM market.security s
        LEFT JOIN LATERAL (
            SELECT fetched_at FROM market.screener_page_raw
            WHERE security_id = s.security_id AND NOT is_blank
            ORDER BY fetched_at DESC LIMIT 1) p ON true
        WHERE s.is_active AND s.series = 'EQ' AND s.security_type = 'equity'
          AND (p.fetched_at IS NULL OR p.fetched_at::date < %s)
    """, ((as_of or date.today()) - timedelta(days=max_age_days),))

    close_batch(db, bid, status="complete" if failed == 0 else "partial",
                total=len(targets), ok=ok, failed=failed, skipped=blank, rows=facts)
    set_watermark(db, SOURCE, "refresh", str(as_of or date.today()), rows=facts)
    return {"targets": len(targets), "refreshed": ok, "blank_quarantined": blank,
            "failed": failed, "facts_written": facts, "still_stale": remaining}


def rebuild_facts_from_payloads(db: Database, *, batch_size: int = 200) -> dict:
    """
    Re-explode screener_fact from the retained page payloads.

    This is why the raw payload is kept. A parser or mapping fix - the growth
    block being skipped, or nulls being dropped - can be replayed over every page
    already collected, with no re-fetching and no dependence on the original JSON
    files still being on disk. It also covers pages recovered by the retry queue,
    which never existed as files.
    """
    from ..ingest.legacy_import import _facts_from_payload

    pages = db.fetch_all("""
        SELECT DISTINCT ON (security_id, basis)
               page_id, security_id, basis, fetched_at, payload
        FROM   market.screener_page_raw
        WHERE  NOT is_blank
        ORDER  BY security_id, basis, fetched_at DESC
    """)
    if not pages:
        return {"pages": 0}

    bid = open_batch(db, SOURCE, {"mode": "rebuild_facts", "pages": len(pages)})
    metrics: dict[str, dict] = {}
    facts: list[tuple] = []

    for p in pages:
        rec = p["payload"]
        if isinstance(rec, str):
            rec = json.loads(rec)

        q_periods = set()
        for series in (rec.get("quarters") or {}).values():
            for lbl in series or {}:
                got = parse_period_label(lbl, "quarter")
                if got:
                    q_periods.add(got[1])
        q_latest = max(q_periods) if q_periods else None

        for stmt in STATEMENT_KEYS:
            for label in (rec.get(stmt) or {}):
                d = mm.describe(stmt, label)
                metrics.setdefault(d["metric_id"], d)
        for label in (rec.get("top_ratios") or {}):
            d = mm.describe("top_ratios", label)
            metrics.setdefault(d["metric_id"], d)
        for table, ranges in (rec.get("growth") or {}).items():
            for rng in (ranges or {}):
                gid = mm.growth_metric_id(table, rng)
                metrics.setdefault(gid, {
                    "metric_id": gid, "metric_label": f"{table} :: {rng}",
                    "statement": "growth", "unit": "pct",
                    "higher_is_better": True, "description": None})

        snap = p["fetched_at"].date()
        for period_type, report_date, mid, value in _facts_from_payload(
                rec, q_latest, snap):
            facts.append((p["security_id"], period_type, report_date, p["basis"],
                          mid, None if value is None else float(value),
                          p["fetched_at"], "screener", p["page_id"], bid))

    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "m_in", {
            "metric_id": "text", "metric_label": "text", "statement": "text",
            "unit": "text", "higher_is_better": "boolean", "description": "text"})
        copy_rows(cur, "m_in", ("metric_id", "metric_label", "statement", "unit",
                                "higher_is_better", "description"),
                  [(d["metric_id"], d["metric_label"], d["statement"], d["unit"],
                    d["higher_is_better"], d["description"]) for d in metrics.values()])
        cur.execute("""
            INSERT INTO market.metric_dim
                (metric_id, metric_label, statement, unit, higher_is_better, description)
            SELECT DISTINCT ON (metric_id) metric_id, metric_label, statement, unit,
                   higher_is_better, description
            FROM staging.m_in ON CONFLICT (metric_id) DO NOTHING
        """)

        create_staging(cur, "f_in", {
            "security_id": "bigint", "period_type": "text", "report_date": "date",
            "statement_basis": "text", "metric_id": "text", "value": "numeric",
            "available_at": "timestamptz", "source": "text", "page_id": "bigint",
            "sync_batch_id": "text"})
        copy_rows(cur, "f_in",
                  ("security_id", "period_type", "report_date", "statement_basis",
                   "metric_id", "value", "available_at", "source", "page_id",
                   "sync_batch_id"), facts)
        cur.execute("""
            INSERT INTO market.screener_fact
                (security_id, period_type, report_date, statement_basis, metric_id,
                 value, available_at, source, page_id, sync_batch_id)
            SELECT DISTINCT ON (security_id, period_type, report_date,
                                statement_basis, metric_id, available_at)
                   security_id, period_type, report_date, statement_basis, metric_id,
                   value, available_at, source, page_id, sync_batch_id
            FROM   staging.f_in
            ON CONFLICT (security_id, period_type, report_date, statement_basis,
                         metric_id, available_at)
            DO UPDATE SET value = EXCLUDED.value, page_id = EXCLUDED.page_id
        """)
        drop_staging(cur, "m_in", "f_in")

    total = db.fetch_value("SELECT count(*) AS c FROM market.screener_fact")
    close_batch(db, bid, status="complete", total=len(pages), ok=len(pages),
                rows=len(facts))
    return {"pages": len(pages), "facts_written": len(facts),
            "metrics": len(metrics), "facts_total": total}


def drain_retry_queue(settings: Settings, db: Database, *, limit: int = 25,
                      sleep_sec: float = 4.0) -> dict:
    """Work due rows from the retry queue. Safe to run repeatedly on a schedule."""
    mark_stale_batches(db)
    reclaimed = reclaim_stale_claims(db)
    rows = claim(db, limit)
    if not rows:
        pending = db.fetch_value(
            "SELECT count(*) AS c FROM market.fetch_retry_queue "
            "WHERE source=%s AND state='pending'", (SOURCE,))
        return {"claimed": 0, "pending": pending, "note": "nothing due yet"}

    bid = open_batch(db, SOURCE, {"mode": "retry_drain", "claimed": len(rows)})
    client = screener_client(settings.screener_session_cookie,
                             min_request_gap_sec=max(sleep_sec, 3.0))
    recovered = still_blank = failed = 0
    facts_written = 0

    for i, r in enumerate(rows, 1):
        attempt = int(r["attempts"])
        # Every attempt after the first starts from a clean session; from the
        # third the user agent rotates too.
        client.reset_session(rotate_user_agent=attempt >= 3)
        client.warmup(force=True)

        rec, err = _fetch(client, r["symbol"], attempt)
        if rec is not None:
            facts_written += _store(db, r["symbol"], rec, bid)
            release(db, r["retry_id"], resolved=True, attempts=attempt)
            recovered += 1
            log.info("recovered %s on attempt %d", r["symbol"], attempt + 1)
        else:
            if err and err.startswith("blank"):
                still_blank += 1
            else:
                failed += 1
                record_error(db, bid, SOURCE, r["symbol"], err or "unknown")
            release(db, r["retry_id"], resolved=False, attempts=attempt, error=err)
        time.sleep(sleep_sec + random.uniform(0, 2.0))

    state = db.fetch_all(
        "SELECT state, count(*) AS n FROM market.fetch_retry_queue "
        "WHERE source=%s GROUP BY state", (SOURCE,))
    close_batch(db, bid, status="complete", total=len(rows), ok=recovered,
                failed=failed, skipped=still_blank, rows=facts_written)
    return {"claimed": len(rows), "reclaimed_stale": reclaimed,
            "recovered": recovered, "still_blank": still_blank,
            "failed": failed, "facts_written": facts_written,
            "queue": {s["state"]: s["n"] for s in state}}
