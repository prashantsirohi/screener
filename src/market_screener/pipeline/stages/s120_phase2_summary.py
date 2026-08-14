"""
Phase 2 summary: what was found, and - as loudly - what could not be.

The section on limits is not boilerplate. A forensic review that never opened a
filing can only report what the statements show, and a reader who takes "no flags
raised" for "clean" has been misled by the document rather than by the data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from ...domain import forensics, phase2, valuation
from ..context import IST, RunContext, StageArtifact, StageResult, sha256_file

log = logging.getLogger(__name__)

STAGE = "s120_phase2_summary"


def run(ctx: RunContext) -> StageResult:
    db = ctx.db
    L: list[str] = []
    A = L.append

    rows = db.fetch_all(
        "SELECT * FROM market.phase2_assessment WHERE run_id = %s", (ctx.run_id,))
    n = len(rows)
    counts = ctx.state.get("verdict_counts") or {}
    selected = ctx.state.get("selected", 0)

    A("# Phase 2 Summary - Forensic and Valuation Validation\n")
    A(f"**Run id:** `{ctx.run_id}`  ")
    A(f"**Phase 1 run reviewed:** `{ctx.state.get('phase1_run_id')}`  ")
    A(f"**As of:** {ctx.as_of} (Asia/Kolkata)  ")
    A(f"**Point-in-time cutoff:** {ctx.pit_cutoff:%Y-%m-%d %H:%M %Z}\n")

    A("## 1. What this phase does, and does not, examine\n")
    A("Phase 1 asks whether a company presents an identifiable return mechanism. "
      "Phase 2 asks two questions it deliberately left alone: are the reported "
      "numbers trustworthy, and is the price sensible.\n")
    A("**It does not read filings.** Auditor qualifications, related-party "
      "transactions, promoter pledging, contingent liabilities and CWIP ageing "
      "are the checks a forensic analyst reaches for first, and every one needs "
      "the annual report. No filing has been fetched by this system. Each "
      "advancing name therefore carries an explicit list of open questions, and "
      "**a clean forensic score means 'nothing visible in the statements', not "
      "'clean'.**\n")
    A("Valuation uses trailing reported figures only. There are no forward "
      "estimates anywhere in this system, so every multiple is historic.\n")

    A("## 2. Funnel\n")
    A("| Stage | Count |")
    A("|---|---:|")
    A(f"| Phase 1 candidates reviewed | {n} |")
    A(f"| Advanced | {counts.get('advance', 0)} |")
    A(f"| Held for a filing | {counts.get('hold', 0)} |")
    A(f"| Rejected | {counts.get('reject', 0)} |")
    A(f"| **Selected for Phase 3** | **{selected}** |")
    A("")
    A(f"Selection: {ctx.state.get('selection_note', 'n/a')}.\n")
    if ctx.state.get("bound_by") == "evidence":
        A("> The evidence bar bound before the target. Fewer names cleared it "
          "than the 40-60 band anticipates, which is a statement about this "
          "candidate set rather than a fault in the run.\n")

    fl = db.fetch_all("""
        SELECT flag_code, severity, count(*) AS n
        FROM   market.phase2_flag WHERE run_id = %s
        GROUP  BY 1, 2 ORDER BY n DESC
    """, (ctx.run_id,))
    A("## 3. Forensic findings\n")
    if not fl:
        A("No forensic flags were raised. Given the limits above, read that as "
          "an absence of evidence rather than evidence of absence.\n")
    else:
        A("| Flag | Severity | Companies | What it means |")
        A("|---|---|---:|---|")
        for f in fl:
            A(f"| `{f['flag_code']}` | {f['severity']} | {f['n']} | "
              f"{forensics.FLAG_MEANINGS.get(f['flag_code'], '')} |")
        A("")

    sev = db.fetch_all("""
        SELECT forensic_severity AS s, count(*) AS n
        FROM   market.phase2_assessment WHERE run_id = %s GROUP BY 1
    """, (ctx.run_id,))
    A("Worst finding per company: "
      + ", ".join(f"{r['s']} {r['n']}" for r in sorted(
          sev, key=lambda r: forensics.SEVERITIES.index(r["s"]))) + ".\n")

    vv = db.fetch_all("""
        SELECT valuation_verdict AS v, count(*) AS n
        FROM   market.phase2_assessment WHERE run_id = %s GROUP BY 1 ORDER BY n DESC
    """, (ctx.run_id,))
    A("## 4. Valuation\n")
    A(f"Five methods per company, kept separate. A single fair value would imply "
      f"a precision trailing aggregator figures cannot support, and where methods "
      f"disagree that disagreement is recorded rather than averaged away. The "
      f"discount rate is a fixed {valuation.COST_OF_EQUITY_PCT:.1f}% cost of "
      f"equity ({valuation.RISK_FREE_PCT:.1f}% risk free + "
      f"{valuation.EQUITY_PREMIUM_PCT:.1f}% premium), stated as an assumption "
      f"rather than derived.\n")
    A("| Overall verdict | Companies |")
    A("|---|---:|")
    for r in vv:
        A(f"| {r['v']} | {r['n']} |")
    A("")

    A("## 5. How the verdict is reached\n")
    A("A **disqualifying** forensic finding rejects a name outright; it is a "
      "gate, not a weighted input. A company whose cumulative cash flow covers "
      "40% of its reported profit should not advance because its P/E is low - "
      "that combination is what a value trap looks like from outside.\n")
    A("Otherwise the verdict is a joint judgement on the statements and the "
      "price, not a threshold on a blended score. You will accept forensic "
      "questions if the price pays you for them, and a full price if the "
      "statements are clean - not both at once.\n")
    A("| statements \\ price | cheap | fair | full | stretched |")
    A("|---|---|---|---|---|")
    for sev, row in phase2.VERDICT_MATRIX.items():
        A(f"| {sev} | " + " | ".join(row) + " |")
    A("")
    A(f"The combined score - {phase2.W_FORENSIC:.0%} forensic, "
      f"{phase2.W_VALUATION:.0%} valuation, {phase2.W_PHASE1:.0%} Phase 1 "
      f"priority - only **ranks** the advancing set. It does not decide "
      f"membership: three already-high inputs average into a narrow band, so a "
      f"bar on it would never bind and the cap would silently do the "
      f"narrowing.\n")

    A("## 6. Files\n")
    A("| File | Contents |")
    A("|---|---|")
    A("| `P2_reviewed.csv` | Every candidate reviewed, with its verdict |")
    A("| `P2_advancing.csv` | The narrowed set, ranked, with open questions |")
    A("| `P2_evidence.csv` | One row per forensic flag and valuation method |")
    A("")
    A("---")
    A("*Analytical research output produced by an automated screen. Not "
      "personalised investment advice.*")

    out = ctx.output_dir()
    md = out / "P2_summary.md"
    md.write_text("\n".join(L), encoding="utf-8")

    files = []
    for name in ("P2_reviewed.csv", "P2_advancing.csv", "P2_evidence.csv",
                 "P2_summary.md"):
        p = out / name
        if p.exists():
            files.append({"name": name, "sha256": sha256_file(p),
                          "bytes": p.stat().st_size})
    manifest = {
        "run_id": ctx.run_id, "phase": 2, "as_of": str(ctx.as_of),
        "phase1_run_id": ctx.state.get("phase1_run_id"),
        "generated_at": datetime.now(IST).isoformat(),
        "config_hash": ctx.settings.config_hash(),
        "reads_primary_filings": False,
        "files": files,
    }
    mp = out / "P2_run_manifest.json"
    mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return StageResult(
        stage=STAGE, rows_out=len(files) + 1,
        artifacts=[StageArtifact("P2_summary.md", "md", md, len(L)),
                   StageArtifact("P2_run_manifest.json", "json", mp, len(files))],
        detail={"files": len(files) + 1})
