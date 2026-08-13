"""
Operational health checks surfaced by `screener status`.

The one that matters most is reconciliation drift. A rising `missed_action`
count means the adjustment is losing track of corporate actions, and that
degrades silently: prices stay plausible, moving averages stay smooth, and the
only visible symptom is that two independently-sourced series stop agreeing.
It is also the single thing standing between the current setup and the
price-return basis, where those securities have no fallback and are excluded
outright.

Each check returns a level and a message. `ok` is reported only in verbose mode;
`warn` and `alert` always print.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..db.connection import Database

# Share of compared securities that may carry a missing action before it is
# treated as a problem rather than background noise.
MISSED_ACTION_WARN_PCT = 10.0
MISSED_ACTION_ALERT_PCT = 20.0
# A rise of this many securities between snapshots is worth surfacing even if
# the overall share is still under the threshold.
MISSED_ACTION_RISE = 25
STALE_WATERMARK_DAYS = 5


@dataclass(frozen=True)
class Check:
    level: str          # ok | warn | alert
    name: str
    message: str
    detail: str | None = None

    @property
    def marker(self) -> str:
        return {"ok": "  ok  ", " warn ": " warn ", "warn": " warn ",
                "alert": "ALERT "}.get(self.level, "  ?   ")


def _verdicts(db: Database, as_of: date) -> dict[str, int]:
    rows = db.fetch_all("""
        SELECT verdict, count(*) AS n FROM market.price_source_reconciliation
        WHERE  as_of_date = %s GROUP BY 1
    """, (as_of,))
    return {r["verdict"]: r["n"] for r in rows}


def reconciliation_snapshots(db: Database) -> list[date]:
    return [r["as_of_date"] for r in db.fetch_all("""
        SELECT DISTINCT as_of_date FROM market.price_source_reconciliation
        ORDER BY as_of_date DESC LIMIT 2
    """)]


def check_reconciliation(db: Database) -> list[Check]:
    """
    Is corporate-action coverage holding, or slipping?

    `missed_action` is a step in the ratio between the two price series - one of
    them applied an adjustment the other did not. Every one is an action the
    pipeline has not found.
    """
    snaps = reconciliation_snapshots(db)
    if not snaps:
        return [Check("warn", "reconciliation",
                      "never run - price sources have not been compared",
                      "run: screener derive --what reconcile")]

    latest = snaps[0]
    cur = _verdicts(db, latest)
    total = sum(cur.values())
    if not total:
        return [Check("warn", "reconciliation", f"no rows for {latest}")]

    # Kept separate on purpose: `missed_action` is a step landing on a ratio a
    # split or bonus actually produces - an action we have demonstrably missed.
    # `disagree` is a series that drifts apart without any such step, which is a
    # data-quality question rather than a missing action. Summing them into one
    # number made the alert impossible to reconcile against the table above it.
    missed = cur.get("missed_action", 0)
    disagree = cur.get("disagree", 0)
    untrusted = missed + disagree
    pct = missed / total * 100

    prev_txt = ""
    delta = None
    if len(snaps) > 1:
        prev = _verdicts(db, snaps[1])
        delta = missed - prev.get("missed_action", 0)
        prev_txt = f", {delta:+d} since {snaps[1]}"

    if pct >= MISSED_ACTION_ALERT_PCT:
        level = "alert"
    elif pct >= MISSED_ACTION_WARN_PCT or (delta is not None and delta >= MISSED_ACTION_RISE):
        level = "warn"
    else:
        level = "ok"

    out = [Check(
        level, "reconciliation",
        f"{missed} securities ({pct:.1f}% of {total}) show a price step matching "
        f"a split or bonus we have not recorded{prev_txt}",
        (f"plus {disagree} that diverge without a clean step; {untrusted} in total "
         f"fall back to Yahoo, and on the price-return basis would be excluded"
         if level != "ok" else None))]

    if delta is not None and delta >= MISSED_ACTION_RISE:
        out.append(Check(
            "warn", "reconciliation trend",
            f"coverage worsened by {delta} securities since {snaps[1]}",
            "try: screener derive --what actions then --what actions-divergence"))
    return out


def worst_unreconciled(db: Database, limit: int = 5) -> list[dict]:
    """The largest unexplained steps - where to look first."""
    return db.fetch_all("""
        SELECT s.symbol, r.verdict, round(r.max_step_pct, 1) AS step_pct,
               r.weeks_compared
        FROM   market.price_source_reconciliation r
        JOIN   market.security s USING (security_id)
        WHERE  r.as_of_date = (SELECT max(as_of_date)
                               FROM market.price_source_reconciliation)
          AND  r.verdict IN ('missed_action', 'disagree')
        ORDER  BY r.max_step_pct DESC NULLS LAST
        LIMIT  %s
    """, (limit,))


def check_retry_queue(db: Database) -> list[Check]:
    rows = db.fetch_all("""
        SELECT state, count(*) AS n FROM market.fetch_retry_queue GROUP BY 1
    """)
    by_state = {r["state"]: r["n"] for r in rows}
    out: list[Check] = []

    exhausted = by_state.get("exhausted", 0)
    if exhausted:
        out.append(Check(
            "alert", "retry queue",
            f"{exhausted} item(s) exhausted after every attempt",
            "these companies are absent from the screen; "
            "check coverage before trusting a run"))

    pending = by_state.get("pending", 0)
    if pending:
        due = db.fetch_value("""
            SELECT count(*) AS c FROM market.fetch_retry_queue
            WHERE state = 'pending' AND next_attempt_at <= now()
        """)
        out.append(Check(
            "warn" if due else "ok", "retry queue",
            f"{pending} pending ({due} due now)",
            "run: screener sync --source fundamentals --retry-queue" if due else None))

    stuck = db.fetch_value("""
        SELECT count(*) AS c FROM market.fetch_retry_queue
        WHERE state = 'in_flight' AND claimed_at < now() - interval '2 hours'
    """)
    if stuck:
        out.append(Check("warn", "retry queue",
                         f"{stuck} claim(s) stranded in_flight",
                         "the next drain reclaims them"))
    return out or [Check("ok", "retry queue", "empty")]


def check_watermarks(db: Database, as_of: date | None = None) -> list[Check]:
    as_of = as_of or date.today()
    rows = db.fetch_all("""
        SELECT source, scope, watermark, last_run_at, last_status
        FROM   market.sync_watermark ORDER BY source, scope
    """)
    out: list[Check] = []
    for r in rows:
        if r["last_status"] not in (None, "complete"):
            out.append(Check("warn", f"sync {r['source']}",
                             f"last run finished '{r['last_status']}'"))
        if r["last_run_at"] and (as_of - r["last_run_at"].date()).days > STALE_WATERMARK_DAYS:
            out.append(Check(
                "warn", f"sync {r['source']}",
                f"not synced for {(as_of - r['last_run_at'].date()).days} days"))
    return out


def check_latest_run(db: Database) -> list[Check]:
    run = db.fetch_one("""
        SELECT run_id, status, as_of_date FROM market.screen_run
        ORDER BY started_at DESC LIMIT 1
    """)
    if not run:
        return [Check("warn", "screen", "no run recorded yet")]
    out: list[Check] = []
    if run["status"] != "complete":
        out.append(Check("alert", "screen",
                         f"last run {run['run_id']} ended '{run['status']}'"))
    failed = db.fetch_all("""
        SELECT check_id, check_name FROM market.screen_qc_result
        WHERE run_id = %s AND NOT passed
    """, (run["run_id"],))
    if failed:
        out.append(Check("alert", "screen QC",
                         f"{len(failed)} check(s) failed on {run['run_id']}",
                         ", ".join(f"{f['check_id']} {f['check_name']}" for f in failed)))
    return out or [Check("ok", "screen", f"{run['run_id']} clean")]


def check_blank_pages(db: Database) -> list[Check]:
    n = db.fetch_value("""
        SELECT count(*) AS c FROM (
            SELECT DISTINCT ON (security_id) security_id, is_blank
            FROM   market.screener_page_raw ORDER BY security_id, fetched_at DESC
        ) latest WHERE is_blank
    """)
    if not n:
        return [Check("ok", "fundamentals", "no companies stuck on a blank page")]
    level = "alert" if n > 100 else "warn"
    return [Check(level, "fundamentals",
                  f"{n} compan(ies) have no usable fundamentals page",
                  "they are excluded by the market-cap gate")]


def run_all(db: Database, as_of: date | None = None) -> list[Check]:
    checks: list[Check] = []
    for fn in (check_reconciliation, check_retry_queue, check_blank_pages,
               check_latest_run):
        try:
            checks.extend(fn(db))
        except Exception as exc:                       # noqa: BLE001
            checks.append(Check("warn", fn.__name__, f"check failed: {exc}"))
    try:
        checks.extend(check_watermarks(db, as_of))
    except Exception as exc:                           # noqa: BLE001
        checks.append(Check("warn", "check_watermarks", f"check failed: {exc}"))
    return checks
