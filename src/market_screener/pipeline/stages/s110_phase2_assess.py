"""
Phase 2 assessment: forensic checks and valuation over the Phase 1 candidates.

Reads the most recent completed Phase 1 run for the same `as_of` and reviews the
names it selected. Nothing is re-screened - Phase 1 owns eligibility and
classification; this stage only asks whether the numbers are trustworthy and
whether the price is sensible.

Point-in-time throughout, on the same cutoff Phase 1 used, so a backdated Phase 2
sees the same world its Phase 1 did.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from ...db.copy_io import copy_rows, create_staging, drop_staging
from ...domain import forensics, fundamentals_view as fv, phase2, valuation
from ...domain import metrics as metrics_mod
from ..context import RunContext, StageResult

log = logging.getLogger(__name__)

STAGE = "s110_phase2_assess"

EPS_LABEL = "EPS in Rs"


class NoPhase1Run(RuntimeError):
    pass


def _phase1_candidates(db, as_of, config_hash: str) -> tuple[str, list[dict]]:
    """
    The candidate set to review, from the latest comparable Phase 1 run.

    Matched on config_hash as well as as_of: reviewing a candidate list produced
    under different gates or a different price basis would silently mix two
    screening regimes in one report.
    """
    run_id = db.fetch_value("""
        SELECT run_id FROM market.screen_run
        WHERE  phase = 1 AND status = 'complete' AND as_of_date = %s
          AND  config_hash = %s
        ORDER  BY started_at DESC LIMIT 1
    """, (as_of, config_hash))
    if not run_id:
        raise NoPhase1Run(
            f"no completed Phase 1 run for as_of={as_of} on config {config_hash}. "
            f"Run `screener screen --as-of {as_of}` first.")

    rows = db.fetch_all("""
        SELECT c.rank AS phase1_rank, u.security_id, u.symbol, u.company,
               u.primary_archetype, u.preliminary_priority_score
        FROM   market.phase1_candidate c
        JOIN   market.phase1_universe u USING (run_id, security_id)
        WHERE  c.run_id = %s
        ORDER  BY c.rank
    """, (run_id,))
    return run_id, rows


def _pe_history(db, security_ids: list[int], payloads: dict,
                as_of) -> dict[int, list[float]]:
    """
    Trailing P/E at each fiscal year end, per company.

    Price comes from the elected adjusted weekly series and EPS from the
    aggregator's restated annual figures, so both sides are split-adjusted and a
    bonus issue does not read as a valuation collapse. It is still an
    approximation: the aggregator restates history, so this is today's view of
    past EPS, not what was reported at the time.
    """
    if not security_ids:
        return {}
    rows = db.fetch_all("""
        SELECT DISTINCT ON (security_id, fy) security_id, fy, close
        FROM (
            SELECT security_id, week_end_date, close,
                   EXTRACT(year FROM week_end_date + interval '9 months')::int AS fy
            FROM   market.weekly_bar_resolved
            WHERE  security_id = ANY(%s) AND week_end_date <= %s
        ) t
        ORDER BY security_id, fy, week_end_date DESC
    """, (security_ids, as_of))

    close_by: dict[int, dict[int, float]] = defaultdict(dict)
    for r in rows:
        if r["close"] is not None:
            close_by[r["security_id"]][int(r["fy"])] = float(r["close"])

    out: dict[int, list[float]] = {}
    for sid in security_ids:
        eps_block = (payloads.get(sid, {}).get("profit_loss") or {}).get(EPS_LABEL) or {}
        series = []
        for col, eps in eps_block.items():
            # Column labels are "Mar 2024"; the year is the fiscal year end.
            parts = col.split()
            if len(parts) != 2 or not parts[1].isdigit() or not eps or eps <= 0:
                continue
            close = close_by.get(sid, {}).get(int(parts[1]))
            if close:
                series.append(close / float(eps))
        if series:
            out[sid] = series
    return out


def reuse(ctx: RunContext, prior_run_id: str) -> StageResult:
    db = ctx.db
    with db.transaction() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market.phase2_assessment
            SELECT %s, security_id, phase1_run_id, phase1_rank, symbol, company,
                   primary_archetype, forensic_score, forensic_severity,
                   flags_raised, valuation_score, valuation_verdict,
                   pe_percentile_5y, implied_growth_pct, combined_score, verdict,
                   verdict_reason, open_questions, rank, assessed_at
            FROM   market.phase2_assessment WHERE run_id = %s
            ON CONFLICT DO NOTHING
        """, (ctx.run_id, prior_run_id))
        for table, cols in (("phase2_flag",
                             "security_id, flag_code, severity, value_num, evidence"),
                            ("phase2_valuation",
                             "security_id, method, value_num, verdict, basis")):
            cur.execute(f"""
                INSERT INTO market.{table} (run_id, {cols})
                SELECT %s, {cols} FROM market.{table} WHERE run_id = %s
                ON CONFLICT DO NOTHING
            """, (ctx.run_id, prior_run_id))

    n = db.fetch_value("SELECT count(*) AS c FROM market.phase2_assessment "
                       "WHERE run_id = %s", (ctx.run_id,))
    ctx.state["reviewed"] = n
    return StageResult(stage=STAGE, status="skipped",
                       skip_reason="unchanged_stage_inputs", rows_out=n,
                       detail={"reviewed": n, "carried_from": prior_run_id})


def run(ctx: RunContext) -> StageResult:
    db, st, as_of = ctx.db, ctx.settings, ctx.as_of
    cutoff = ctx.pit_cutoff

    p1_run, candidates = _phase1_candidates(db, as_of, st.config_hash())
    log.info("reviewing %d candidates from %s", len(candidates), p1_run)
    ctx.state["phase1_run_id"] = p1_run

    labels = fv._label_lookup(db)
    payloads = fv.payloads_for_universe(db, as_of=cutoff, labels=labels)
    sids = [c["security_id"] for c in candidates]
    pe_hist = _pe_history(db, sids, payloads, as_of)

    rows, flag_rows, val_rows = [], [], []
    cleared = 0

    for c in candidates:
        sid = c["security_id"]
        payload = payloads.get(sid) or {"error": "no payload"}
        m = metrics_mod.compute(payload, None)

        flags = forensics.assess(payload, m)
        severity = forensics.severity_of(flags)
        f_score = forensics.score(flags)

        methods, v_score, v_verdict = valuation.assess(m, pe_hist.get(sid))
        # numeric columns arrive as Decimal; the domain layer works in float and
        # mixing the two raises rather than coercing.
        p1_score = c["preliminary_priority_score"]
        verdict = phase2.decide(f_score, severity, v_score, v_verdict,
                                float(p1_score) if p1_score is not None else None,
                                methods)
        if verdict.verdict == "advance":
            cleared += 1

        pe_pct = next((mm.value for mm in methods
                       if mm.name == "pe_percentile_5y"), None)
        implied = next((mm.value for mm in methods
                        if mm.name == "reverse_dcf_growth"), None)

        rows.append((
            ctx.run_id, sid, p1_run, c["phase1_rank"], c["symbol"], c["company"],
            c["primary_archetype"], f_score, severity, len(flags),
            v_score, v_verdict, pe_pct, implied,
            verdict.combined_score, verdict.verdict, verdict.reason,
            phase2.open_questions(c["primary_archetype"], flags), None))

        for f in flags:
            flag_rows.append((ctx.run_id, sid, f.code, f.severity, f.value,
                              f.evidence))
        for mm in methods:
            val_rows.append((ctx.run_id, sid, mm.name, mm.value, mm.verdict,
                             mm.basis))

    _write(ctx, rows, flag_rows, val_rows)
    ctx.state["reviewed"] = len(rows)
    ctx.state["cleared"] = cleared

    return StageResult(stage=STAGE, rows_in=len(candidates), rows_out=len(rows),
                       detail={"reviewed": len(rows), "cleared": cleared,
                               "flags": len(flag_rows),
                               "phase1_run": p1_run})


ASSESS_COLS = (
    "run_id", "security_id", "phase1_run_id", "phase1_rank", "symbol", "company",
    "primary_archetype", "forensic_score", "forensic_severity", "flags_raised",
    "valuation_score", "valuation_verdict", "pe_percentile_5y",
    "implied_growth_pct", "combined_score", "verdict", "verdict_reason",
    "open_questions", "rank",
)


def _write(ctx: RunContext, rows, flag_rows, val_rows) -> None:
    db = ctx.db
    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "p2_a", {
            "run_id": "text", "security_id": "bigint", "phase1_run_id": "text",
            "phase1_rank": "integer", "symbol": "text", "company": "text",
            "primary_archetype": "text", "forensic_score": "numeric",
            "forensic_severity": "text", "flags_raised": "integer",
            "valuation_score": "numeric", "valuation_verdict": "text",
            "pe_percentile_5y": "numeric", "implied_growth_pct": "numeric",
            "combined_score": "numeric", "verdict": "text",
            "verdict_reason": "text", "open_questions": "text", "rank": "integer"})
        copy_rows(cur, "p2_a", ASSESS_COLS, rows)
        cur.execute(f"""
            INSERT INTO market.phase2_assessment ({", ".join(ASSESS_COLS)})
            SELECT {", ".join(ASSESS_COLS)} FROM staging.p2_a
            ON CONFLICT (run_id, security_id) DO NOTHING
        """)

        create_staging(cur, "p2_f", {
            "run_id": "text", "security_id": "bigint", "flag_code": "text",
            "severity": "text", "value_num": "numeric", "evidence": "text"})
        copy_rows(cur, "p2_f", ("run_id", "security_id", "flag_code", "severity",
                                "value_num", "evidence"), flag_rows)
        cur.execute("""
            INSERT INTO market.phase2_flag
                (run_id, security_id, flag_code, severity, value_num, evidence)
            SELECT run_id, security_id, flag_code, severity, value_num, evidence
            FROM   staging.p2_f ON CONFLICT DO NOTHING
        """)

        create_staging(cur, "p2_v", {
            "run_id": "text", "security_id": "bigint", "method": "text",
            "value_num": "numeric", "verdict": "text", "basis": "text"})
        copy_rows(cur, "p2_v", ("run_id", "security_id", "method", "value_num",
                                "verdict", "basis"), val_rows)
        cur.execute("""
            INSERT INTO market.phase2_valuation
                (run_id, security_id, method, value_num, verdict, basis)
            SELECT run_id, security_id, method, value_num, verdict, basis
            FROM   staging.p2_v ON CONFLICT DO NOTHING
        """)
        drop_staging(cur, "p2_a", "p2_f", "p2_v")
