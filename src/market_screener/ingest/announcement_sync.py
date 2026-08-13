"""
Incremental corporate-announcement sync.

Walks forward from the watermark in windows, hash-deduping on write. Windows
deliberately overlap by a day: NSE occasionally back-dates a filing, and the
content hash makes a re-read free.

New announcements are classified immediately under both taxonomy versions, so
the event flags the screen reads never lag the feed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone

from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from ..domain.taxonomy import classify_v1, classify_v2
from ..http.client import nse_client
from ..http.errors import HttpError
from .sync_state import (close_batch, get_watermark, mark_stale_batches,
                         open_batch, record_error, set_watermark)

log = logging.getLogger(__name__)

SOURCE = "events.nse_announcements"
URL = "https://www.nseindia.com/api/corporate-announcements"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
IST = timezone(timedelta(hours=5, minutes=30))

WINDOW_DAYS = 20
# One day of overlap so a back-dated filing is not missed at a window seam.
OVERLAP_DAYS = 1


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _hash(symbol: str, subject, announced, attachment, external_id) -> str:
    blob = "|".join([
        "nse_announcements", symbol, _norm(subject),
        announced.isoformat() if announced else "",
        str(attachment or ""), str(external_id or ""),
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_dt(v):
    if not v:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(v).strip(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def sync(db: Database, *, backfill_days: int | None = None,
         until: date | None = None, max_windows: int | None = None) -> dict:
    mark_stale_batches(db)
    until = until or date.today()

    wm = get_watermark(db, SOURCE)
    if backfill_days:
        start = until - timedelta(days=backfill_days)
    elif wm:
        start = date.fromisoformat(str(wm)[:10]) - timedelta(days=OVERLAP_DAYS)
    else:
        start = until - timedelta(days=30)

    if start > until:
        return {"status": "current", "watermark": wm, "windows": 0}

    bid = open_batch(db, SOURCE, {"start": str(start), "until": str(until)})
    http = nse_client(referer=REFERER)
    http.warmup()

    smap = {r["symbol"]: r["security_id"] for r in db.fetch_all(
        "SELECT symbol, security_id FROM market.security WHERE exchange = 'NSE'")}
    alias = {r["symbol"]: r["security_id"] for r in db.fetch_all(
        "SELECT symbol, security_id FROM market.security_alias WHERE exchange = 'NSE'")}

    windows, fetched_rows, failed = 0, 0, 0
    new_hashes: list[str] = []
    cur_start = start

    while cur_start <= until:
        win_end = min(cur_start + timedelta(days=WINDOW_DAYS), until)
        if max_windows and windows >= max_windows:
            break
        try:
            payload = http.fetch_with_retries(
                lambda: http.get_json(URL, params={
                    "index": "equities",
                    "from_date": cur_start.strftime("%d-%m-%Y"),
                    "to_date": win_end.strftime("%d-%m-%Y")},
                    headers={"Referer": REFERER}),
                description=f"announcements {cur_start}..{win_end}")
        except HttpError as exc:
            record_error(db, bid, SOURCE, f"{cur_start}..{win_end}", str(exc))
            failed += 1
            cur_start = win_end + timedelta(days=1)
            continue

        rows_in = payload.get("data") if isinstance(payload, dict) else payload
        rows_in = rows_in or []
        log.info("announcements %s..%s: %d rows", cur_start, win_end, len(rows_in))

        staged = []
        for r in rows_in:
            sym = str(r.get("symbol") or "").strip()
            if not sym:
                continue
            subject = r.get("desc")
            body = r.get("attchmntText")
            att = r.get("attchmntFile")
            announced = _parse_dt(r.get("an_dt") or r.get("dt"))
            if announced is None:
                continue
            h = _hash(sym, subject, announced, att, r.get("seqId"))
            staged.append((
                h, smap.get(sym) or alias.get(sym), sym,
                str(r.get("sm_isin") or "").strip() or None,
                "nse_announcements", str(r.get("seqId") or "").strip() or None,
                str(subject)[:2000] if subject else None,
                str(body)[:8000] if body else None,
                announced, announced,
                str(att) if att else None, bid))

        if staged:
            with db.transaction() as conn, conn.cursor() as cur:
                create_staging(cur, "ann_new", {
                    "announcement_hash": "text", "security_id": "bigint",
                    "raw_symbol": "text", "raw_isin": "text", "source": "text",
                    "external_id": "text", "subject": "text", "description": "text",
                    "announced_at": "timestamptz", "available_at": "timestamptz",
                    "attachment_url": "text", "sync_batch_id": "text"})
                copy_rows(cur, "ann_new", (
                    "announcement_hash", "security_id", "raw_symbol", "raw_isin",
                    "source", "external_id", "subject", "description",
                    "announced_at", "available_at", "attachment_url",
                    "sync_batch_id"), staged)
                cur.execute("""
                    INSERT INTO market.announcement
                        (announcement_hash, security_id, raw_symbol, raw_isin, source,
                         external_id, subject, description, announced_at,
                         available_at, attachment_url, sync_batch_id)
                    SELECT DISTINCT ON (announcement_hash)
                           announcement_hash, security_id, raw_symbol, raw_isin, source,
                           external_id, subject, description, announced_at,
                           available_at, attachment_url, sync_batch_id
                    FROM   staging.ann_new
                    ON CONFLICT (announcement_hash) DO UPDATE SET
                        seen_count = market.announcement.seen_count + 1,
                        last_seen_at = now()
                    RETURNING announcement_hash
                """)
                new_hashes.extend(r[0] if isinstance(r, tuple) else r["announcement_hash"]
                                  for r in cur.fetchall())
                drop_staging(cur, "ann_new")
            fetched_rows += len(staged)

        windows += 1
        cur_start = win_end + timedelta(days=1)

    classified = _classify_new(db, new_hashes)

    latest = db.fetch_value("SELECT max(announced_at) AS m FROM market.announcement")
    total = db.fetch_value("SELECT count(*) AS c FROM market.announcement")
    status = "complete" if failed == 0 else "partial"
    close_batch(db, bid, status=status, total=windows, ok=windows - failed,
                failed=failed, rows=fetched_rows)
    set_watermark(db, SOURCE, "*",
                  latest.date().isoformat() if latest else None,
                  status=status, rows=fetched_rows)

    return {"status": status, "windows": windows, "rows_seen": fetched_rows,
            "classified": classified, "stored_total": total,
            "watermark": str(latest) if latest else None, "failed_windows": failed}


def _classify_new(db: Database, hashes: list[str]) -> int:
    """Classify only what this run touched, under both taxonomy versions."""
    if not hashes:
        return 0
    rows = db.fetch_all("""
        SELECT announcement_hash, subject, description
        FROM   market.announcement WHERE announcement_hash = ANY(%s)
    """, (hashes,))
    out = []
    for r in rows:
        # Same column order as the bulk classifier: attachment text then subject.
        blob = f"{r['description'] or ''} {r['subject'] or ''}"
        for label, _tag in classify_v1(blob):
            out.append((r["announcement_hash"], f"v1:{label}", label, None, None, None))
        c = classify_v2(blob)
        out.append((r["announcement_hash"], "v2", c.category, c.tier,
                    c.importance, c.matched))
    if not out:
        return 0
    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "cls_new", {
            "announcement_hash": "text", "taxonomy_version": "text",
            "primary_category": "text", "tier": "text", "importance": "numeric",
            "matched_keyword": "text"})
        copy_rows(cur, "cls_new", ("announcement_hash", "taxonomy_version",
                                   "primary_category", "tier", "importance",
                                   "matched_keyword"), out)
        cur.execute("""
            INSERT INTO market.announcement_classification
                (announcement_hash, taxonomy_version, primary_category, tier,
                 importance, matched_keyword)
            SELECT DISTINCT ON (announcement_hash, taxonomy_version)
                   announcement_hash, taxonomy_version, primary_category, tier,
                   importance, matched_keyword
            FROM   staging.cls_new
            ON CONFLICT (announcement_hash, taxonomy_version) DO UPDATE SET
                primary_category = EXCLUDED.primary_category,
                tier = EXCLUDED.tier, importance = EXCLUDED.importance,
                classified_at = now()
        """)
        drop_staging(cur, "cls_new")
    return len(out)
