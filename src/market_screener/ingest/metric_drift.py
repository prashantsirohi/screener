"""
Detect the aggregator renaming, dropping or re-basing a statement row.

`metric_id` is a slug of the source's DISPLAY label. Display labels are not an
interface and they move - "Sales" is already "Revenue" for lenders. When a row
is renamed the slug changes, a new metric_id appears, and the old one silently
stops receiving facts. Nothing errors: every ratio built on the old id just
becomes None for every company, which reads exactly like a company that does not
report it.

So the drift has to be observed rather than caught. `snapshot()` records what
exists now; `drift()` compares the two most recent snapshots and reports the four
changes worth acting on:

    appeared      a label we have never mapped before
    vanished      a metric that had facts and now has none
    unit_changed  the same id now carries a different unit
    coverage_drop a metric that lost a large share of its companies

`mapping_version` is what keeps this honest. It fingerprints our own alias and
unit tables, so a label change under an unchanged mapping_version is the
source's doing, and a change accompanied by a new mapping_version is ours.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..db.connection import Database
from ..domain import metric_map as mm

log = logging.getLogger(__name__)

# A metric losing this share of its companies between snapshots is reported.
# Coverage moves for benign reasons - a backfill, a batch of recovered pages -
# so the threshold is deliberately high enough that routine growth is quiet.
COVERAGE_DROP_PCT = 25.0
# Below this, percentage moves are noise: a metric held by 4 companies losing
# one is a 25% "collapse" that means nothing.
MIN_SECURITIES = 20


def observe(db: Database) -> list[dict]:
    """What the fact store currently holds, per metric."""
    return db.fetch_all("""
        SELECT f.metric_id, d.statement, d.metric_label, d.unit,
               count(DISTINCT f.security_id) AS securities,
               count(*)                      AS facts
        FROM   market.screener_fact f
        JOIN   market.metric_dim d USING (metric_id)
        GROUP  BY 1, 2, 3, 4
    """)


def snapshot(db: Database, *, at: datetime | None = None) -> dict:
    """Record the current observation. Idempotent per timestamp."""
    at = at or datetime.now(timezone.utc)
    rows = observe(db)
    if not rows:
        return {"metrics": 0, "snapshot_at": at}

    version = mm.mapping_version()
    with db.transaction() as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO market.metric_coverage_snapshot
                (snapshot_at, metric_id, statement, metric_label, unit,
                 mapping_version, securities, facts)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (snapshot_at, metric_id) DO UPDATE SET
                securities = EXCLUDED.securities, facts = EXCLUDED.facts
        """, [(at, r["metric_id"], r["statement"], r["metric_label"], r["unit"],
               version, r["securities"], r["facts"]) for r in rows])
    return {"metrics": len(rows), "snapshot_at": at,
            "mapping_version": version}


def _snapshot_times(db: Database) -> list[datetime]:
    return [r["snapshot_at"] for r in db.fetch_all("""
        SELECT DISTINCT snapshot_at FROM market.metric_coverage_snapshot
        ORDER BY snapshot_at DESC LIMIT 2
    """)]


def drift(db: Database) -> dict:
    """
    Compare the two most recent snapshots.

    Returns a report rather than raising: a renamed label is information for an
    operator, not a reason to abort a run that is otherwise fine.
    """
    times = _snapshot_times(db)
    if len(times) < 2:
        return {"status": "insufficient_history", "snapshots": len(times)}

    now_at, prev_at = times[0], times[1]
    rows = db.fetch_all("""
        SELECT snapshot_at, metric_id, statement, metric_label, unit,
               mapping_version, securities
        FROM   market.metric_coverage_snapshot
        WHERE  snapshot_at IN (%s, %s)
    """, (now_at, prev_at))

    report = compare([r for r in rows if r["snapshot_at"] == prev_at],
                     [r for r in rows if r["snapshot_at"] == now_at])
    report["compared"] = (prev_at, now_at)
    return report


def compare(before: list[dict], after: list[dict]) -> dict:
    """
    The comparison itself, over plain rows.

    Separated from the query so it can be tested against fabricated drift. A
    detector nobody has watched fire is not a detector - and this codebase has
    already shipped two checks that could not fail.
    """
    old = {r["metric_id"]: r for r in before}
    cur = {r["metric_id"]: r for r in after}

    versions = {r["mapping_version"] for r in (*before, *after)
                if r.get("mapping_version")}
    our_change = len(versions) > 1

    appeared = [cur[m] for m in sorted(set(cur) - set(old))]
    vanished = [old[m] for m in sorted(set(old) - set(cur))]

    unit_changed, coverage_drop = [], []
    for mid in sorted(set(cur) & set(old)):
        a, b = old[mid], cur[mid]
        if a["unit"] != b["unit"]:
            unit_changed.append({"metric_id": mid, "was": a["unit"],
                                 "now": b["unit"], "label": b["metric_label"]})
        if a["securities"] >= MIN_SECURITIES:
            drop = 100.0 * (a["securities"] - b["securities"]) / a["securities"]
            if drop >= COVERAGE_DROP_PCT:
                coverage_drop.append({
                    "metric_id": mid, "label": b["metric_label"],
                    "was": a["securities"], "now": b["securities"],
                    "drop_pct": round(drop, 1)})

    # A rename shows up as one metric vanishing and another appearing in the
    # same statement at the same time. Pairing them turns two confusing lines
    # into the one fact an operator needs.
    renames = []
    for v in vanished:
        for a in appeared:
            if a["statement"] == v["statement"] and \
                    abs(a["securities"] - v["securities"]) <= max(
                        5, 0.05 * v["securities"]):
                renames.append({"from": v["metric_id"], "to": a["metric_id"],
                                "from_label": v["metric_label"],
                                "to_label": a["metric_label"],
                                "securities": a["securities"]})
    return {
        "status": "ok",
        "mapping_changed_by_us": our_change,
        "appeared": appeared,
        "vanished": vanished,
        "unit_changed": unit_changed,
        "coverage_drop": coverage_drop,
        "likely_renames": renames,
    }


def has_findings(report: dict) -> bool:
    return report.get("status") == "ok" and any(
        report[k] for k in ("appeared", "vanished", "unit_changed",
                            "coverage_drop"))
