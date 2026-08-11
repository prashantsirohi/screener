"""
Shared sync bookkeeping: batches, watermarks, errors, stale-run recovery.

Every collector opens a batch, records per-item errors against it, and closes it
with counts. A batch left in 'running' means the process died - the reaper
converts those to 'interrupted' at the start of the next run so the state is
honest rather than permanently ambiguous.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from ..db.connection import Database

log = logging.getLogger(__name__)

STALE_RUNNING_MINUTES = 120


def batch_id(source: str) -> str:
    return f"{source.replace('_', '-')}-{uuid.uuid4().hex[:10]}"


def mark_stale_batches(db: Database, minutes: int = STALE_RUNNING_MINUTES) -> int:
    """A batch still 'running' after `minutes` belongs to a dead process."""
    n = db.execute(
        "UPDATE market.sync_batch "
        "SET status = 'interrupted', "
        "    finished_at = COALESCE(finished_at, now()), "
        "    note = COALESCE(note, '') || ' [reaped: no completion recorded]' "
        "WHERE status = 'running' "
        "  AND started_at < now() - make_interval(mins => %s)",
        (minutes,))
    if n:
        log.warning("marked %d stale running batch(es) as interrupted", n)
    return n


def open_batch(db: Database, source: str, params: dict | None = None) -> str:
    bid = batch_id(source)
    db.execute(
        "INSERT INTO market.sync_batch (sync_batch_id, source, params) VALUES (%s,%s,%s)",
        (bid, source, json.dumps(params or {}, default=str)))
    return bid


def close_batch(db: Database, bid: str, *, status: str = "complete",
                total: int = 0, ok: int = 0, failed: int = 0, skipped: int = 0,
                rows: int = 0, note: str | None = None) -> None:
    db.execute(
        "UPDATE market.sync_batch SET finished_at = now(), status = %s, "
        "items_total = %s, items_succeeded = %s, items_failed = %s, "
        "items_skipped = %s, rows_written = %s, note = %s WHERE sync_batch_id = %s",
        (status, total, ok, failed, skipped, rows, note, bid))


def fail_batch(db: Database, bid: str, error: str) -> None:
    db.execute(
        "UPDATE market.sync_batch SET finished_at = now(), status = 'failed', "
        "note = %s WHERE sync_batch_id = %s", (error[:2000], bid))


def record_error(db: Database, bid: str | None, source: str, scope: str | None,
                 error: str, error_class: str | None = None) -> None:
    db.execute(
        "INSERT INTO market.sync_error (sync_batch_id, source, scope, error_class, error) "
        "VALUES (%s,%s,%s,%s,%s)", (bid, source, scope, error_class, str(error)[:4000]))


def get_watermark(db: Database, source: str, scope: str = "*") -> str | None:
    return db.fetch_value(
        "SELECT watermark FROM market.sync_watermark WHERE source = %s AND scope = %s",
        (source, scope))


def set_watermark(db: Database, source: str, scope: str, watermark: str | None,
                  *, status: str = "complete", rows: int = 0,
                  note: str | None = None) -> None:
    """Upsert. rows_written accumulates; watermark is replaced."""
    db.execute(
        "INSERT INTO market.sync_watermark "
        "  (source, scope, watermark, last_run_at, last_status, rows_written, note, updated_at) "
        "VALUES (%s,%s,%s, now(), %s, %s, %s, now()) "
        "ON CONFLICT (source, scope) DO UPDATE SET "
        "  watermark = EXCLUDED.watermark, last_run_at = EXCLUDED.last_run_at, "
        "  last_status = EXCLUDED.last_status, "
        "  rows_written = market.sync_watermark.rows_written + EXCLUDED.rows_written, "
        "  note = EXCLUDED.note, updated_at = now()",
        (source, scope, watermark, status, rows, note))


def latest_batch_status(db: Database) -> list[dict[str, Any]]:
    return db.fetch_all("""
        SELECT DISTINCT ON (source)
               source, sync_batch_id, status, started_at, finished_at, rows_written
        FROM   market.sync_batch
        ORDER  BY source, started_at DESC
    """)
