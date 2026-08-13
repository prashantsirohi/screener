"""
Collect primary-document links for shortlisted companies.

screener.in's documents block links straight through to the filings themselves -
BSE-hosted annual report PDFs, quarterly result filings, board-meeting outcomes
and CARE/CRISIL/ICRA rating rationales. Those are primary sources, and they are
what Phase 2 must verify material claims against.

Phase 1 only discovers and records the links; nothing is downloaded. Scoped to
the current candidate set by default, because there is no reason to crawl 2,086
companies to support a 150-name shortlist.
"""

from __future__ import annotations

import logging
import random
import re
import time

from lxml import html as lhtml

from ..config import Settings
from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from ..http.client import screener_client
from ..http.errors import HttpError
from .sync_state import (close_batch, mark_stale_batches, open_batch,
                         record_error, set_watermark)

log = logging.getLogger(__name__)

SOURCE = "documents.screener"

FY_RE = re.compile(r"financial year (\d{4})", re.I)
YEAR_RE = re.compile(r"(20\d{2})")


def classify(text: str, href: str) -> str:
    t, h = (text or "").lower(), (href or "").lower()
    if FY_RE.search(t) or "annualreport" in h:
        return "annual_report"
    if "rating" in t or any(k in h for k in ("careratings", "crisil", "icra")):
        return "rating"
    if "transcript" in t:
        return "transcript"
    if "ppt" in t or "presentation" in t:
        return "presentation"
    if "results" in t:
        return "results"
    if "board meeting" in t:
        return "filing"
    if "drhp" in t or "sebi.gov.in" in h:
        return "offer_document"
    return "filing"


def period_of(text: str) -> str | None:
    m = FY_RE.search(text or "")
    if m:
        return f"FY{m.group(1)}"
    m = YEAR_RE.search(text or "")
    return m.group(1) if m else None


def _scrape(client, symbol: str) -> list[dict]:
    for path in (f"https://www.screener.in/company/{symbol}/consolidated/",
                 f"https://www.screener.in/company/{symbol}/"):
        try:
            r = client.get(path)
        except HttpError:
            continue
        if "/register/" in r.url or "/login/" in r.url:
            continue
        doc = lhtml.fromstring(r.text)
        out, seen = [], set()
        for sec_id in ("documents", "concalls"):
            for sec in doc.cssselect(f"section#{sec_id}"):
                for a in sec.cssselect("a"):
                    href = a.get("href", "")
                    if not href or href.startswith("#"):
                        continue
                    url = href if href.startswith("http") else \
                        f"https://www.screener.in{href}"
                    if url in seen:
                        continue
                    seen.add(url)
                    txt = " ".join(a.text_content().split())[:200]
                    if not txt:
                        continue
                    out.append({"url": url, "title": txt,
                                "doc_type": classify(txt, href),
                                "period": period_of(txt)})
        if out:
            return out
    return []


def sync(settings: Settings, db: Database, *, run_id: str | None = None,
         limit: int | None = None, sleep_sec: float = 2.5) -> dict:
    """Collect document links for the latest run's candidates (or a capped set)."""
    mark_stale_batches(db)

    if run_id is None:
        run_id = db.fetch_value("""
            SELECT run_id FROM market.screen_run
            WHERE  phase = 1 AND status = 'complete'
            ORDER  BY started_at DESC LIMIT 1
        """)
    if not run_id:
        return {"targets": 0, "note": "no completed run to take a candidate set from"}

    targets = db.fetch_all("""
        SELECT u.security_id, u.symbol
        FROM   market.phase1_candidate c
        JOIN   market.phase1_universe u USING (run_id, security_id)
        WHERE  c.run_id = %s
          AND  NOT EXISTS (SELECT 1 FROM market.document d
                           WHERE d.security_id = u.security_id)
        ORDER  BY c.rank
        LIMIT  %s
    """, (run_id, limit or 200))
    if not targets:
        return {"targets": 0, "note": "every candidate already has documents"}

    bid = open_batch(db, SOURCE, {"run_id": run_id, "targets": len(targets)})
    client = screener_client(settings.screener_session_cookie,
                             min_request_gap_sec=max(sleep_sec, 2.0))
    client.warmup()

    rows, ok, empty, failed = [], 0, 0, 0
    for i, t in enumerate(targets, 1):
        try:
            docs = _scrape(client, t["symbol"])
        except Exception as exc:                      # noqa: BLE001
            record_error(db, bid, SOURCE, t["symbol"], str(exc))
            failed += 1
            continue
        if not docs:
            empty += 1
        else:
            ok += 1
            for d in docs:
                issuer = ("BSE" if "bseindia" in d["url"]
                          else "NSE" if "nseindia" in d["url"]
                          else "screener.in")
                rows.append((t["security_id"], d["doc_type"], d["title"],
                             d["period"], d["url"], issuer))
        if i % 20 == 0:
            log.info("documents %d/%d (ok=%d empty=%d failed=%d)",
                     i, len(targets), ok, empty, failed)
        time.sleep(sleep_sec + random.uniform(0, 1.0))

    if rows:
        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "doc_in", {
                "security_id": "bigint", "doc_type": "text", "title": "text",
                "period": "text", "url": "text", "issuer": "text"})
            copy_rows(cur, "doc_in",
                      ("security_id", "doc_type", "title", "period", "url",
                       "issuer"), rows)
            cur.execute("""
                INSERT INTO market.document
                    (security_id, doc_type, title, period, url, issuer)
                SELECT DISTINCT ON (url)
                       security_id, doc_type, title, period, url, issuer
                FROM   staging.doc_in
                ON CONFLICT (url) DO NOTHING
            """)
            drop_staging(cur, "doc_in")

    by_type = db.fetch_all(
        "SELECT doc_type, count(*) AS n FROM market.document GROUP BY 1 ORDER BY n DESC")
    close_batch(db, bid, status="complete" if failed == 0 else "partial",
                total=len(targets), ok=ok, failed=failed, skipped=empty,
                rows=len(rows))
    set_watermark(db, SOURCE, run_id, str(len(rows)), rows=len(rows))
    return {"targets": len(targets), "with_documents": ok, "none_found": empty,
            "failed": failed, "links_written": len(rows),
            "by_type": {r["doc_type"]: r["n"] for r in by_type}}
