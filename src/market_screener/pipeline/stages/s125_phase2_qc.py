"""
Phase 2 QC.

Written to the same standard the Phase 1 checks were eventually held to: every
check must be able to FAIL on real data. Two Phase 1 checks once queried a table
nothing wrote and passed vacuously for months, so each check here either counts
something that exists or asserts a relationship that could genuinely break.
"""

from __future__ import annotations

import hashlib
import json
import logging

from ...domain import forensics, phase2, valuation
from ..context import RunContext, StageResult

log = logging.getLogger(__name__)

STAGE = "s125_phase2_qc"


def run(ctx: RunContext) -> StageResult:
    db = ctx.db
    results: list[tuple[str, str, bool, str]] = []

    def check(cid, name, ok, detail=""):
        results.append((cid, name, bool(ok), detail))

    rows = db.fetch_all(
        "SELECT * FROM market.phase2_assessment WHERE run_id = %s", (ctx.run_id,))
    n = len(rows)
    advancing = [r for r in rows if r["verdict"] == "advance"]
    ranked = [r for r in rows if r["rank"] is not None]

    p1_count = db.fetch_value("""
        SELECT count(*) AS c FROM market.phase1_candidate WHERE run_id = %s
    """, (ctx.state.get("phase1_run_id"),))
    check("P2QC01", "Every Phase 1 candidate was reviewed",
          n > 0 and n == p1_count, f"{n} reviewed against {p1_count} candidates")

    check("P2QC02", "Every assessment carries a verdict from the controlled set",
          all(r["verdict"] in ("advance", "hold", "reject") for r in rows),
          f"{len({r['verdict'] for r in rows})} distinct verdicts")

    bad_sev = [r["symbol"] for r in rows
               if r["forensic_severity"] not in forensics.SEVERITIES]
    check("P2QC03", "Forensic severities come from the controlled vocabulary",
          not bad_sev, f"unknown: {bad_sev[:5]}")

    bad_v = [r["symbol"] for r in rows
             if r["valuation_verdict"] not in valuation.VERDICTS]
    check("P2QC04", "Valuation verdicts come from the controlled vocabulary",
          not bad_v, f"unknown: {bad_v[:5]}")

    # The gate that makes Phase 2 mean anything: price cannot buy past a
    # disqualifying finding.
    leaked = [r["symbol"] for r in rows
              if r["forensic_severity"] == "disqualifying"
              and r["verdict"] == "advance"]
    check("P2QC05", "No disqualifying forensic finding advanced", not leaked,
          f"leaked: {leaked[:5]}")

    # Every flag must be one the module can explain, or the summary renders a
    # blank meaning column and the reader cannot act on it.
    codes = {r["flag_code"] for r in db.fetch_all(
        "SELECT DISTINCT flag_code FROM market.phase2_flag WHERE run_id = %s",
        (ctx.run_id,))}
    unknown = sorted(codes - set(forensics.FLAG_MEANINGS))
    check("P2QC06", "Every raised flag has a documented meaning", not unknown,
          f"{len(codes)} distinct flags; undocumented: {unknown}")

    # A flag count that disagrees with the flags table means one of the two was
    # written wrong, and the CSV and the evidence file would not reconcile.
    mismatched = db.fetch_value("""
        SELECT count(*) AS c FROM market.phase2_assessment a
        WHERE  a.run_id = %s AND a.flags_raised <> (
            SELECT count(*) FROM market.phase2_flag f
            WHERE  f.run_id = a.run_id AND f.security_id = a.security_id)
    """, (ctx.run_id,))
    check("P2QC07", "flags_raised reconciles with the evidence table",
          mismatched == 0, f"{mismatched} rows disagree")

    # Five methods are attempted for everyone; unassessable is a recorded answer,
    # not a missing row.
    short = db.fetch_value("""
        SELECT count(*) AS c FROM (
            SELECT security_id, count(*) AS m FROM market.phase2_valuation
            WHERE  run_id = %s GROUP BY 1 HAVING count(*) <> 5
        ) x
    """, (ctx.run_id,))
    check("P2QC08", "Every company has all five valuation methods recorded",
          short == 0, f"{short} companies with a different count")

    check("P2QC09", "Ranks are dense and start at 1",
          sorted(r["rank"] for r in ranked) == list(range(1, len(ranked) + 1)),
          f"{len(ranked)} ranked")

    check("P2QC10", "Only advancing names are ranked",
          all(r["verdict"] == "advance" for r in ranked),
          f"{len(ranked)} ranked, {len(advancing)} advancing")

    # Short lists are legitimate when the evidence bar bound, so this only fails
    # on the impossible case: more selected than actually advanced.
    sel = ctx.state.get("selected", len(ranked))
    check("P2QC11", "Selection size is explained by the target or the evidence",
          sel <= min(len(advancing), phase2.TARGET_HIGH),
          f"{sel} selected, {len(advancing)} advanced, "
          f"bound by {ctx.state.get('bound_by')}")

    # The honesty check. Every advancing name must carry its open questions, or
    # the report implies a completeness it does not have.
    missing_q = [r["symbol"] for r in advancing if not (r["open_questions"] or "").strip()]
    check("P2QC12", "Every advancing name records what a filing would still answer",
          not missing_q, f"missing: {missing_q[:5]}")

    manifest_path = ctx.output_dir() / "P2_run_manifest.json"
    ok_manifest = False
    if manifest_path.exists():
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
        ok_manifest = man.get("reads_primary_filings") is False and all(
            hashlib.sha256((ctx.output_dir() / f["name"]).read_bytes()).hexdigest()
            == f["sha256"] for f in man["files"])
    check("P2QC13", "Manifest checksums reconcile and disclaim filing coverage",
          ok_manifest, f"manifest present={manifest_path.exists()}")

    with db.transaction() as conn, conn.cursor() as cur:
        for cid, name, ok, detail in results:
            cur.execute("""
                INSERT INTO market.screen_qc_result
                    (run_id, check_id, check_name, passed, detail)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (run_id, check_id) DO UPDATE SET
                    passed = EXCLUDED.passed, detail = EXCLUDED.detail,
                    checked_at = now()
            """, (ctx.run_id, cid, name, ok, detail))

    failed = [r for r in results if not r[2]]
    for cid, name, ok, detail in results:
        log.log(logging.INFO if ok else logging.ERROR, "[%s] %s %s%s",
                "PASS" if ok else "FAIL", cid, name, f" - {detail}" if detail else "")

    return StageResult(
        stage=STAGE, status="complete" if not failed else "failed",
        rows_out=len(results),
        error=None if not failed else f"{len(failed)} QC check(s) failed",
        detail={"passed": len(results) - len(failed), "total": len(results),
                "failed": [f"{r[0]} {r[1]}" for r in failed]})
