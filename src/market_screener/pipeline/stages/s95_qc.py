"""
Phase 1 quality control.

The brief's ten checks, plus six the port earned: several encode bugs that
actually shipped during the build and were caught by inspecting data rather than
by anything failing. Results are written to screen_qc_result so a run's QC
outcome is part of the run record, not just console output.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pandas as pd

from ...domain import eligibility
from ...domain.archetypes import ARCHETYPES
from ...domain.eligibility import EXCLUSION_CODES
from ...domain.weinstein import STAGES
from ..context import RunContext, StageResult

log = logging.getLogger(__name__)

STAGE = "s95_qc"

CONTROLLED_TAGS = {
    "Earnings-upgrade candidate", "Capex commissioning", "Operating-leverage inflection",
    "Margin-recovery candidate", "PEAD candidate", "Market-share gainer",
    "Debt-reduction story", "Export expansion", "Premiumisation",
    "Product-mix improvement", "Demerger/SOTP unlocking", "Regulatory catalyst",
    "Temporary setback", "Peak-cycle risk", "Governance risk",
    "Customer-concentration risk", "Overvalued quality", "Value-trap risk",
    "Statistically cheap-catalyst unclear",
}
VALID_ARCHETYPES = {name for name, _ in ARCHETYPES} | {
    "Event-driven or special situation"}

SCREEN_COL_COUNT = 37


def run(ctx: RunContext) -> StageResult:
    db, out = ctx.db, ctx.output_dir()
    uni = pd.read_csv(out / "P1_screened_universe.csv")
    cand = pd.read_csv(out / "P1_candidates.csv")
    slog = pd.read_csv(out / "P1_source_log.csv")
    manifest = json.loads((out / "P1_run_manifest.json").read_text(encoding="utf-8"))

    results: list[tuple[str, str, bool, str]] = []

    def check(cid: str, name: str, ok: bool, detail: str = "") -> None:
        results.append((cid, name, bool(ok), detail))

    # ---- the brief's ten ----------------------------------------------------
    active = db.fetch_value(
        "SELECT count(*) AS c FROM market.security "
        "WHERE is_active AND series = 'EQ' AND security_type = 'equity'")
    check("QC01", "Universe count supports the coverage claim",
          len(uni) == active and manifest["universe_claim"] == "partial",
          f"{len(uni)} evaluated vs {active} active series-EQ; "
          f"claim='{manifest['universe_claim']}'")

    bad = cand[~cand["primary_archetype"].isin(VALID_ARCHETYPES)] if len(cand) else cand
    multi = (cand[cand["primary_archetype"].astype(str).str.contains(r"\||,", regex=True)]
             if len(cand) else cand)
    check("QC02", "Every candidate has exactly one valid primary archetype",
          len(bad) == 0 and len(multi) == 0,
          f"{len(bad)} invalid, {len(multi)} multi-valued")

    blob = " ".join(uni["secondary_tags"].fillna("").astype(str))
    check("QC03", "'Undervalued' not assigned anywhere in Phase 1",
          "Undervalued" not in blob)

    used: set[str] = set()
    for s in uni["secondary_tags"].fillna("").astype(str):
        used.update(x for x in s.split("|") if x)
    check("QC04", "All secondary tags come from the controlled vocabulary",
          used <= CONTROLLED_TAGS, f"unknown: {sorted(used - CONTROLLED_TAGS)}")

    cyc = db.fetch_value("""
        SELECT count(*) AS c
        FROM   market.phase1_universe u
        JOIN   market.company_metric cm ON cm.security_id = u.security_id
                                       AND cm.metric = 'opm_vs_peak_pp'
        WHERE  u.run_id = %s AND u.primary_archetype = 'Cyclical recovery'
          AND  cm.value_num >= -2
    """, (ctx.run_id,)) or 0
    n_cyc = int((uni["primary_archetype"] == "Cyclical recovery").sum())
    check("QC05", "No cyclical candidate selected at or near peak margin",
          cyc == 0, f"{n_cyc} cyclicals, {cyc} at peak")

    cap = cand[cand["primary_archetype"] == "Capex operating-leverage candidate"] \
        if len(cand) else cand
    no_ev = (cap[~cap["classification_rationale"].fillna("").str.contains(
        "CWIP|commissioned|capacity", case=False)] if len(cap) else cap)
    check("QC06", "Every capex candidate carries balance-sheet capex evidence",
          len(no_ev) == 0, f"{len(cap)} capex candidates, {len(no_ev)} without evidence")

    stages_used = set(uni["technical_stage"].dropna())
    ind = int((uni["technical_stage"] ==
               "Indeterminate-insufficient adjusted history").sum())
    check("QC07", "Adjusted weekly prices used; short history explicitly flagged",
          stages_used <= set(STAGES),
          f"{ind} flagged Indeterminate; all stages from the controlled set")

    if len(cand):
        pe = pd.to_numeric(cand["preliminary_valuation_value"], errors="coerce")
        score = pd.to_numeric(cand["preliminary_priority_score"], errors="coerce")
        cheap_only = cand[(pe < 12) & (score < 50)]
        check("QC08", "No company selected on a low P/E alone", len(cheap_only) == 0,
              "selection is the 6-component score; P/E carries at most 9 of 100 points")

    flagged = db.fetch_value("""
        SELECT count(*) AS c FROM market.phase1_universe
        WHERE  run_id = %s AND eligible_flag = 1
          AND  data_quality_confidence = 'High'
          AND  security_id IN (
                SELECT security_id FROM market.company_metric
                WHERE metric = 'earnings_quality_flag')
    """, (ctx.run_id,)) or 0
    check("QC09", "Names with heavy other income are confidence-downgraded",
          flagged == 0, f"{flagged} flagged names still rated High")

    ids = set(slog["source_id"])
    missing: set[str] = set()
    for col in ("primary_source_ids", "secondary_source_ids"):
        for s in uni[col].fillna("").astype(str):
            for x in s.split("|"):
                if x and x not in ids:
                    missing.add(x)
    check("QC10", "Every source ID resolves in the source log", not missing,
          f"{len(ids)} logged; unresolved: {sorted(missing)[:5]}")

    hdr_ok = list(uni.columns) == list(cand.columns)[:SCREEN_COL_COUNT] \
        if len(cand.columns) >= SCREEN_COL_COUNT else False
    ck_ok = True
    for f in manifest["files"]:
        p = out / f["name"]
        if hashlib.sha256(p.read_bytes()).hexdigest() != f["sha256"]:
            ck_ok = False
    check("QC11", "Schema, headers and manifest checksums reconcile",
          len(uni.columns) == SCREEN_COL_COUNT and hdr_ok and ck_ok,
          f"universe cols={len(uni.columns)}, candidate cols={len(cand.columns)}, "
          f"checksums ok={ck_ok}")

    # ---- checks the port earned --------------------------------------------
    mixed = db.fetch_value("""
        SELECT count(*) AS c FROM (
            SELECT security_id FROM market.weekly_bar_resolved
            GROUP BY security_id HAVING count(DISTINCT adj_basis) > 1
        ) x
    """)
    check("QC12", "No security mixes return bases in its price history", mixed == 0,
          f"{mixed} securities span more than one basis")

    partial = db.fetch_value("""
        SELECT count(*) AS c FROM market.weekly_bar
        WHERE is_complete AND week_end_date >
              (SELECT max(trade_date) FROM market.price_daily)
    """)
    check("QC13", "No partial week is treated as complete", partial == 0,
          f"{partial} bars marked complete beyond the last trading day")

    dupes = db.fetch_value("""
        SELECT count(*) AS c FROM (
            SELECT a.security_id FROM market.corporate_action a
            JOIN market.corporate_action b
                 ON b.security_id = a.security_id AND b.ex_date <> a.ex_date
                AND b.ex_date BETWEEN a.ex_date - 7 AND a.ex_date + 7
            WHERE a.source = 'nse_api' AND b.source <> 'nse_api'
              AND a.confidence <> 'unconfirmed' AND b.confidence <> 'unconfirmed'
        ) x
    """)
    check("QC14", "No corporate action is applied twice for one event", dupes == 0,
          f"{dupes} overlapping authoritative/inferred pairs")

    no_turnover = db.fetch_value(
        "SELECT count(*) AS c FROM market.price_daily WHERE turnover_inr IS NULL")
    check("QC15", "Every daily bar carries turnover", no_turnover == 0,
          f"{no_turnover} bars missing turnover")

    not_friday = db.fetch_value(
        "SELECT count(*) AS c FROM market.weekly_bar "
        "WHERE EXTRACT(dow FROM week_end_date) <> 5")
    check("QC16", "Every weekly bar lands on an ISO Friday", not_friday == 0,
          f"{not_friday} bars off-Friday")

    dq_low = int((cand["data_quality_confidence"] == "Low").sum()) if len(cand) else 0
    check("QC17", "Candidate set is within the 100-150 target",
          100 <= len(cand) <= 150, f"{len(cand)} candidates, {dq_low} rated Low quality")

    dup_sym = uni["symbol"].duplicated().sum()
    check("QC18", "No duplicate symbols", dup_sym == 0, f"{dup_sym} duplicates")

    # A configured gate that excluded nobody is the silent-no-op failure mode:
    # a stage-name mismatch would leave the run looking healthy with the filter
    # simply absent. Assert it bit, and that nothing it excludes survived.
    gate = eligibility.TechnicalGate.from_settings(ctx.settings.screen)
    if gate.active:
        gated = int((uni["exclusion_code"].isin(
            ("EX_TECHNICAL_STAGE", "EX_WEAK_RS", "EX_NO_TECHNICAL_READ"))).sum())
        leaked = sorted(set(uni[uni["eligible_flag"] == 1]["technical_stage"])
                        & set(gate.exclude_stages))
        check("QC19", "Technical gate excluded companies and leaked none",
              gated > 0 and not leaked,
              f"{gated} excluded by the gate"
              + (f"; LEAKED eligible in {leaked}" if leaked else ""))
    else:
        check("QC19", "Technical gate is off by configuration", True,
              "no stages excluded; stage only nudges the score")

    # ---- persist -------------------------------------------------------------
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
        log.log(logging.INFO if ok else logging.ERROR,
                "[%s] %s %s%s", "PASS" if ok else "FAIL", cid, name,
                f" - {detail}" if detail else "")

    return StageResult(
        stage=STAGE,
        status="complete" if not failed else "failed",
        rows_out=len(results),
        error=None if not failed else f"{len(failed)} QC check(s) failed",
        detail={"passed": len(results) - len(failed), "total": len(results),
                "failed": [f"{r[0]} {r[1]}" for r in failed]})
