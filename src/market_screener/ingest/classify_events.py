"""
Classify stored announcements into event categories.

Runs both taxonomy versions and stores them side by side. v1 reproduces the
frozen Phase 1 baseline; v2 is the richer tiered taxonomy. Nothing switches over
until the difference has been looked at - `diff_versions()` produces that report.

v1 is multi-label (one announcement can be both a demerger and an equity raise),
so it is stored as one row per matched category with a synthetic version key.
v2 is single-label, first match wins.
"""

from __future__ import annotations

import logging
from datetime import date

from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from ..domain.taxonomy import classify_v1, classify_v2, tier_of
from .sync_state import close_batch, open_batch

log = logging.getLogger(__name__)

SOURCE = "events.classify"
BATCH = 25_000


def classify_all(db: Database, *, versions: tuple[str, ...] = ("v1", "v2")) -> dict:
    total = db.fetch_value("SELECT count(*) AS c FROM market.announcement")
    if not total:
        return {"announcements": 0}

    bid = open_batch(db, SOURCE, {"versions": list(versions), "announcements": total})
    written = {v: 0 for v in versions}
    offset = 0

    while True:
        rows = db.fetch_all("""
            SELECT announcement_hash, subject, description
            FROM   market.announcement
            ORDER  BY announcement_hash
            LIMIT  %s OFFSET %s
        """, (BATCH, offset))
        if not rows:
            break

        out: list[tuple] = []
        for r in rows:
            # Order matters. The legacy classifier joined the CSV columns in file
            # order - attchmntText, then desc - and several v1 patterns are
            # proximity-bounded (\bsebi\b.{0,60}(order|penalt|...)). Reversing the
            # two silently loses matches that straddle the boundary, which cost
            # 39 "Regulatory action" flags before this was pinned down.
            # `description` holds attchmntText; `subject` holds desc.
            blob = f"{r['description'] or ''} {r['subject'] or ''}"
            if "v1" in versions:
                for label, _tag in classify_v1(blob):
                    out.append((r["announcement_hash"], "v1", label, None, None, None))
            if "v2" in versions:
                c = classify_v2(blob)
                out.append((r["announcement_hash"], "v2", c.category, c.tier,
                            c.importance, c.matched))

        if out:
            with db.transaction() as conn, conn.cursor() as cur:
                create_staging(cur, "cls_in", {
                    "announcement_hash": "text", "taxonomy_version": "text",
                    "primary_category": "text", "tier": "text",
                    "importance": "numeric", "matched_keyword": "text"})
                copy_rows(cur, "cls_in",
                          ("announcement_hash", "taxonomy_version", "primary_category",
                           "tier", "importance", "matched_keyword"), out)
                # v1 is multi-label, so the PK (hash, version) cannot hold every
                # match. The version key carries the category for v1 rows.
                cur.execute("""
                    INSERT INTO market.announcement_classification
                        (announcement_hash, taxonomy_version, primary_category,
                         tier, importance, matched_keyword)
                    SELECT DISTINCT ON (announcement_hash, taxonomy_version, primary_category)
                           announcement_hash,
                           CASE WHEN taxonomy_version = 'v1'
                                THEN 'v1:' || primary_category ELSE taxonomy_version END,
                           primary_category, tier, importance, matched_keyword
                    FROM   staging.cls_in
                    ON CONFLICT (announcement_hash, taxonomy_version) DO UPDATE SET
                        primary_category = EXCLUDED.primary_category,
                        tier = EXCLUDED.tier, importance = EXCLUDED.importance,
                        matched_keyword = EXCLUDED.matched_keyword,
                        classified_at = now()
                """)
                drop_staging(cur, "cls_in")
            for v in versions:
                written[v] += sum(1 for o in out if o[1] == v)

        offset += BATCH
        if offset % 100_000 == 0:
            log.info("classified %d/%d announcements", offset, total)

    close_batch(db, bid, status="complete", total=total, ok=total,
                rows=sum(written.values()))
    return {"announcements": total, "rows_written": written}


def relink_announcements(db: Database) -> dict:
    """
    Attach announcements to securities registered after they were imported.

    Announcements were loaded before the three-year bhavcopy backfill, so a
    symbol the backfill later registered (a delisted name, say) still carries a
    NULL security_id and drops out of every event query. Resolves by symbol and
    then by historical alias.
    """
    by_symbol = db.execute("""
        UPDATE market.announcement a
        SET    security_id = s.security_id
        FROM   market.security s
        WHERE  a.security_id IS NULL
          AND  s.exchange = 'NSE' AND s.symbol = a.raw_symbol
    """)
    by_alias = db.execute("""
        UPDATE market.announcement a
        SET    security_id = al.security_id
        FROM   market.security_alias al
        WHERE  a.security_id IS NULL
          AND  al.exchange = 'NSE' AND al.symbol = a.raw_symbol
    """)
    by_isin = db.execute("""
        UPDATE market.announcement a
        SET    security_id = s.security_id
        FROM   market.security s
        WHERE  a.security_id IS NULL
          AND  a.raw_isin IS NOT NULL AND s.isin = a.raw_isin
    """)
    remaining = db.fetch_one("""
        SELECT count(*) AS rows, count(DISTINCT raw_symbol) AS symbols
        FROM   market.announcement WHERE security_id IS NULL
    """)
    return {"linked_by_symbol": by_symbol, "linked_by_alias": by_alias,
            "linked_by_isin": by_isin,
            "still_unlinked_rows": remaining["rows"],
            "still_unlinked_symbols": remaining["symbols"]}


def event_flags(db: Database, version: str = "v1") -> list[dict]:
    """
    Latest event per (security, category) - the shape the screen consumes.

    Mirrors the legacy `event_flags.csv`: one row per symbol and event class,
    carrying the most recent occurrence.
    """
    like = "v1:%" if version == "v1" else version
    op = "LIKE" if version == "v1" else "="
    return db.fetch_all(f"""
        SELECT DISTINCT ON (a.security_id, c.primary_category)
               a.security_id, s.symbol, c.primary_category AS event_class,
               c.tier, c.importance, a.announced_at AS latest_date,
               a.subject AS headline
        FROM   market.announcement_classification c
        JOIN   market.announcement a USING (announcement_hash)
        JOIN   market.security s ON s.security_id = a.security_id
        WHERE  c.taxonomy_version {op} %s
          AND  a.security_id IS NOT NULL
        ORDER  BY a.security_id, c.primary_category, a.announced_at DESC
    """, (like,))


def diff_versions(db: Database) -> dict:
    """How the richer taxonomy reclassifies what v1 saw, and what it adds."""
    v1 = db.fetch_all("""
        SELECT primary_category, count(*) AS n
        FROM   market.announcement_classification
        WHERE  taxonomy_version LIKE 'v1:%' GROUP BY 1 ORDER BY n DESC
    """)
    v2 = db.fetch_all("""
        SELECT primary_category, tier, count(*) AS n
        FROM   market.announcement_classification
        WHERE  taxonomy_version = 'v2' GROUP BY 1, 2 ORDER BY n DESC
    """)
    overlap = db.fetch_all("""
        SELECT c1.primary_category AS v1_category,
               c2.primary_category AS v2_category,
               count(*) AS n
        FROM   market.announcement_classification c1
        JOIN   market.announcement_classification c2
               ON c2.announcement_hash = c1.announcement_hash
              AND c2.taxonomy_version = 'v2'
        WHERE  c1.taxonomy_version LIKE 'v1:%'
        GROUP  BY 1, 2 ORDER BY n DESC LIMIT 20
    """)
    return {"v1": {r["primary_category"]: r["n"] for r in v1},
            "v2": {r["primary_category"]: r["n"] for r in v2},
            "v1_to_v2": [(r["v1_category"], r["v2_category"], r["n"]) for r in overlap]}
