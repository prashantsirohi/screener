"""
Phase 2 outputs: rank the advancing set and emit the hand-off to Phase 3.

Three files, mirroring the Phase 1 contract's shape so a reader who knows one
knows the other:

    P2_reviewed.csv     every candidate reviewed, with its verdict
    P2_advancing.csv    the narrowed set, ranked, with open questions
    P2_evidence.csv     one row per forensic flag and valuation method

The evidence file is separate and long-form on purpose. A verdict without its
evidence is an opinion, and packing eight flags into one cell makes "how many
names failed on cash conversion" unanswerable.
"""

from __future__ import annotations

import logging

import pandas as pd

from ...domain import phase2
from ..context import RunContext, StageArtifact, StageResult

log = logging.getLogger(__name__)

STAGE = "s115_phase2_outputs"

REVIEWED_COLS = [
    "symbol", "company", "primary_archetype", "phase1_rank",
    "forensic_score", "forensic_severity", "flags_raised",
    "valuation_score", "valuation_verdict", "pe_percentile_5y",
    "implied_growth_pct", "combined_score", "verdict", "verdict_reason",
]
ADVANCING_EXTRA = ["rank", "open_questions"]


def run(ctx: RunContext) -> StageResult:
    db = ctx.db
    rows = db.fetch_all("""
        SELECT * FROM market.phase2_assessment WHERE run_id = %s
        ORDER BY phase1_rank
    """, (ctx.run_id,))
    if not rows:
        return StageResult(stage=STAGE, status="failed",
                           error="no assessments for this run")

    selected, bound_by = phase2.select(rows)
    for i, r in enumerate(selected, 1):
        r["rank"] = i

    if selected:
        with db.transaction() as conn, conn.cursor() as cur:
            cur.executemany("""
                UPDATE market.phase2_assessment SET rank = %s
                WHERE run_id = %s AND security_id = %s
            """, [(r["rank"], ctx.run_id, r["security_id"]) for r in selected])

    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    note = (f"top {len(selected)} of {counts.get('advance', 0)} advancing, "
            f"capped at the {phase2.TARGET_HIGH} target"
            if bound_by == "target" else
            f"all {len(selected)} names that cleared the evidence bar - "
            f"fewer than the {phase2.TARGET_LOW}-{phase2.TARGET_HIGH} target")
    ctx.state["selected"] = len(selected)
    ctx.state["bound_by"] = bound_by
    ctx.state["selection_note"] = note
    ctx.state["verdict_counts"] = counts

    out = ctx.output_dir()
    artifacts = []

    rev = pd.DataFrame(rows)[REVIEWED_COLS]
    p = out / "P2_reviewed.csv"
    rev.to_csv(p, index=False)
    artifacts.append(StageArtifact("P2_reviewed.csv", "csv", p, len(rev)))

    adv = pd.DataFrame(selected)[REVIEWED_COLS + ADVANCING_EXTRA] \
        if selected else pd.DataFrame(columns=REVIEWED_COLS + ADVANCING_EXTRA)
    p = out / "P2_advancing.csv"
    adv.to_csv(p, index=False)
    artifacts.append(StageArtifact("P2_advancing.csv", "csv", p, len(adv)))

    ev = db.fetch_all("""
        SELECT a.symbol, 'forensic_flag' AS kind, f.flag_code AS item,
               f.severity AS assessment, f.value_num, f.evidence
        FROM   market.phase2_flag f
        JOIN   market.phase2_assessment a USING (run_id, security_id)
        WHERE  f.run_id = %s
        UNION ALL
        SELECT a.symbol, 'valuation' AS kind, v.method AS item,
               v.verdict AS assessment, v.value_num, v.basis AS evidence
        FROM   market.phase2_valuation v
        JOIN   market.phase2_assessment a USING (run_id, security_id)
        WHERE  v.run_id = %s
        ORDER  BY 1, 2, 3
    """, (ctx.run_id, ctx.run_id))
    evd = pd.DataFrame(ev)
    p = out / "P2_evidence.csv"
    evd.to_csv(p, index=False)
    artifacts.append(StageArtifact("P2_evidence.csv", "csv", p, len(evd)))

    log.info("selected %d (%s); %d evidence rows", len(selected), note, len(evd))
    return StageResult(stage=STAGE, rows_in=len(rows), rows_out=len(selected),
                       artifacts=artifacts,
                       detail={"selected": len(selected), "bound_by": bound_by,
                               "verdicts": counts, "evidence_rows": len(evd)})
