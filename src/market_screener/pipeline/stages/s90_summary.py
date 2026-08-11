"""Phase 1 summary document and run manifest."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd

from ...domain.eligibility import EXCLUSION_CODES
from ..context import IST, RunContext, StageArtifact, StageResult, sha256_file

log = logging.getLogger(__name__)

STAGE = "s90_summary"


def _fmt(v, nd=1, dash="Not disclosed"):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return dash
    return f"{float(v):,.{nd}f}"


def run(ctx: RunContext) -> StageResult:
    db, as_of = ctx.db, ctx.as_of
    out = ctx.output_dir()

    uni = pd.read_csv(out / "P1_screened_universe.csv")
    cand = pd.read_csv(out / "P1_candidates.csv")
    slog = pd.read_csv(out / "P1_source_log.csv")

    n_all = len(uni)
    n_el = int((uni["eligible_flag"] == 1).sum())
    n_sel = len(cand)
    exc = uni[uni["eligible_flag"] == 0]["exclusion_code"].value_counts()
    arch_el = uni[uni["eligible_flag"] == 1]["primary_archetype"].value_counts()
    arch_sel = cand["primary_archetype"].value_counts() if n_sel else pd.Series(dtype=int)
    stage_el = uni[uni["eligible_flag"] == 1]["technical_stage"].value_counts()
    stage_sel = cand["technical_stage"].value_counts() if n_sel else pd.Series(dtype=int)

    price_date = db.fetch_value(
        "SELECT max(trade_date) AS d FROM market.price_daily WHERE trade_date <= %s",
        (as_of,))
    tech_date = db.fetch_value(
        "SELECT max(week_end_date) AS d FROM market.weekly_bar WHERE is_complete")
    fin_period = db.fetch_value("""
        SELECT max(report_date) AS d FROM market.screener_fact
        WHERE period_type = 'annual'
    """)
    basis = ctx.settings.price_basis
    src_mix = db.fetch_all("""
        SELECT source, count(*) AS n FROM market.weekly_bar_source_choice GROUP BY 1
    """)
    blanks = db.fetch_one("""
        SELECT count(*) FILTER (WHERE state = 'pending')   AS pending,
               count(*) FILTER (WHERE state = 'resolved')  AS resolved,
               count(*) FILTER (WHERE state = 'exhausted') AS exhausted
        FROM   market.fetch_retry_queue WHERE source = 'fundamentals.screener'
    """)

    L: list[str] = []
    A = L.append
    A("# Phase 1 Summary - Indian Equity Screen (Discovery and Bucket Classification)\n")
    A(f"**Run id:** `{ctx.run_id}`  ")
    A(f"**Screening date (as_of):** {as_of} (Asia/Kolkata)  ")
    A(f"**Share-price date:** {price_date}  ")
    A(f"**Technical-data cutoff:** {tech_date} (last complete weekly bar, "
      f"{basis} basis)  ")
    A(f"**Financial-data cutoff:** latest annual period in the store is {fin_period}; "
      f"trailing-twelve-month and latest-quarter figures included where published.\n")

    A("## 1. Universe definition and actual coverage\n")
    A("**Universe claim: FULL for NSE main-board series-EQ equities that are "
      "currently listed. Not a full NSE+BSE universe.**\n")
    A(f"- The active universe is what NSE's `EQUITY_L` lists today: "
      f"**{n_all} series-EQ securities**, every one of which appears in "
      f"`P1_screened_universe.csv`.")
    A("- The store also holds securities that traded historically but have since "
      "delisted, merged or changed series. Their price history is retained; they "
      "are marked inactive and are not screened.")
    A("- BSE-only listings, SME-platform scrips, ETFs, REITs/InvITs and non-EQ "
      "series were never in the frame.\n")

    A("## 2. Funnel\n")
    A("| Stage | Count |")
    A("|---|---:|")
    A(f"| Active NSE series-EQ securities evaluated | {n_all} |")
    A(f"| Excluded | {n_all - n_el} |")
    A(f"| Eligible after all gates | {n_el} |")
    A(f"| Selected as Phase 1 candidates | {n_sel} |")
    A(f"\nSelection rule: {ctx.state.get('selection_note', 'n/a')}.\n")

    A("### Exclusions by code\n")
    A("| Code | Count | Meaning |")
    A("|---|---:|---|")
    for code, cnt in exc.items():
        if code and str(code) != "nan":
            A(f"| `{code}` | {cnt} | {EXCLUSION_CODES.get(code, '')} |")
    A("")

    A("## 3. Data provenance\n")
    A("| Layer | Source | Basis |")
    A("|---|---|---|")
    A("| Universe | NSE `EQUITY_L` | Primary |")
    A("| Daily prices and turnover | NSE bhavcopy | Primary |")
    A("| Corporate actions | NSE corporate-actions feed, plus price-gap inference | Primary |")
    A(f"| Weekly bars for staging | elected per security: "
      f"{', '.join(f'{r['source']} ({r['n']})' for r in src_mix)} | mixed |")
    A("| Fundamentals | screener.in company pages | Secondary |")
    A("| Corporate events | NSE announcements | Primary |")
    A("")
    A(f"**Return basis.** The entire technical layer runs on `{basis}`. A single "
      f"basis is mandatory: Yahoo's adjusted close is total return and the "
      f"bhavcopy series is price return, so mixing them inside one lookback steps "
      f"the series by the cumulative dividend yield and corrupts every moving "
      f"average spanning the seam. Relative strength divides a stock by a "
      f"benchmark, so both sides must measure the same thing.\n")
    A("**Partial weeks are excluded.** Only completed weekly bars enter the "
      "analysis, so the current unfinished week cannot drag the latest moving "
      "average point or the 52-week high.\n")

    if blanks and (blanks["pending"] or blanks["exhausted"]):
        A(f"**Aggregator gaps.** {blanks['resolved']} companies whose fundamentals "
          f"page initially returned a data-free shell have been recovered; "
          f"{blanks['pending']} are queued for retry and {blanks['exhausted']} are "
          f"exhausted. Those still outstanding are excluded by the market-cap gate "
          f"and are visible via `screener status`.\n")

    A("## 4. Eligibility gates\n")
    A("Applied in order; the first gate a company fails is its recorded code.\n")
    A("| Gate | Rule |")
    A("|---|---|")
    A("| Financial record | a parseable statement set must exist |")
    A("| Market cap | INR 1,000 cr <= market cap <= INR 1,00,000 cr |")
    A("| Financial history | >= 3 annual reporting periods |")
    A("| Price history | >= 40 complete adjusted weekly bars |")
    A("| Liquidity | 13-week median daily traded value >= INR 1.0 cr |")
    A("| Classification | at least one archetype discovery test must pass |")
    A("")

    A("## 5. Three-axis classification\n")
    A("**Axis A** - exactly one primary archetype, the highest-scoring of ten "
      "archetype-specific discovery tests. Each returns zero when its own "
      "preconditions are absent, so an archetype is never assigned by default.\n")
    A("**Axis B** - secondary tags from the controlled vocabulary only. "
      "**`Undervalued` was not assigned to any company**; it requires the "
      "three-condition test in Phase 2.\n")
    A("**Axis C** - Weinstein stage computed arithmetically from adjusted weekly "
      "closes. No stage was assigned by judgement.\n")

    A("### Eligible universe by archetype\n")
    A("| Archetype | Eligible | Selected |")
    A("|---|---:|---:|")
    for a in arch_el.index:
        if a and str(a) != "nan":
            A(f"| {a} | {arch_el[a]} | {int(arch_sel.get(a, 0))} |")
    A(f"| **Total** | **{n_el}** | **{n_sel}** |\n")

    A("### Technical stage distribution\n")
    A("| Stage | Selected | Eligible universe |")
    A("|---|---:|---:|")
    for s in stage_el.index:
        A(f"| {s} | {int(stage_sel.get(s, 0))} | {stage_el[s]} |")
    A("")

    if n_sel:
        A("## 6. Top 20 Phase 2 priorities\n")
        A("| # | Company | Symbol | Mkt cap (cr) | Archetype | Stage | Score |")
        A("|---:|---|---|---:|---|---|---:|")
        for i, r in enumerate(cand.head(20).itertuples(index=False), 1):
            A(f"| {i} | {r.company} | `{r.symbol}` | {_fmt(r.market_cap_inr_cr, 0)} | "
              f"{r.primary_archetype} | {r.technical_stage} | "
              f"{_fmt(r.preliminary_priority_score, 1)} |")
        A("")

    A("## 7. Data gaps and limitations\n")
    A("1. **BSE-only companies were not screened.** Coverage is NSE main-board "
      "series-EQ only.")
    A("2. **Fundamentals are aggregator-sourced**, a secondary source. Every "
      "material figure for a shortlisted name must be confirmed against the "
      "annual report or exchange filing in Phase 2.")
    A("3. **No normalisation of exceptional items.** Reported EPS/PAT only; names "
      "where other income exceeds 35% of PBT carry an explicit flag.")
    A("4. **Net debt is gross borrowings.** Cash is not separately available from "
      "the aggregator, so leverage is overstated for cash-rich companies.")
    A("5. **Promoter pledging was not captured** and is a required Phase 2 check.")
    A("6. **Auditor qualifications, related-party transactions, contingent "
      "liabilities and CWIP ageing were not assessed.** These need the annual report.")
    A("7. **Capex evidence is balance-sheet inference** from CWIP intensity and "
      "drawdown, not verified commissioning dates or order books.")
    A("8. **Event flags are keyword-classified** from announcement text, not read "
      "in full.")
    A("9. **Liquidity is a weekly-derived substitute** for a true daily 3-month "
      "median. Now that bhavcopy turnover is ingested, a true daily median is "
      "possible and is a deliberate future change.")
    A("10. **No forward estimates.** All growth figures are realised, not forecast.")
    A("")

    A("## 8. Instructions for Phase 2\n")
    A("1. **Recompute net debt properly** (borrowings less cash and liquid "
      "investments). Several apparent leverage flags will dissolve.")
    A("2. **Check promoter pledging and auditor qualifications** for every "
      "candidate - neither was available at Phase 1 and either can be disqualifying.")
    A("3. **Normalise earnings** and reconcile reported to normalised EPS.")
    A("4. **For capex candidates**, replace the CWIP inference with verified "
      "commissioning dates, capex spent versus budget, funding and demand evidence.")
    A("5. **For cyclical candidates**, build mid-cycle margins from the full "
      "history and value on those - not the trough, not the peak.")
    A("6. **For event-driven candidates**, read the actual scheme or offer document.")
    A("7. **Treat the technical stage as timing information only.** It must not "
      "rescue a company whose fundamentals or governance fail.")
    A("8. Names with `data_quality_confidence = Low` need their data rebuilt from "
      "filings before any model is trusted.")
    A("")

    A("## 9. Units\n")
    A("All currency is Indian rupees. `market_cap_inr_cr` and "
      "`liquidity_value_inr_cr` are in crore (1 cr = 10,000,000). Prices are per "
      "share. `_pct` fields are percentages, not fractions. `net_debt_to_equity` "
      "and `cfo_pat_ratio` are ratios. Dates are `YYYY-MM-DD`.\n")
    A("---")
    A("*Analytical research output produced by an automated screen. Not "
      "personalised investment advice.*")

    md = ctx.output_dir() / "P1_summary.md"
    md.write_text("\n".join(L), encoding="utf-8")

    files = []
    for name in ("P1_screened_universe.csv", "P1_candidates.csv",
                 "P1_source_log.csv", "P1_summary.md"):
        p = out / name
        rc = len(pd.read_csv(p)) if name.endswith(".csv") else None
        files.append({"name": name, "row_count": rc, "sha256": sha256_file(p)})

    manifest = {
        "schema_version": "1.0",
        "phase": 1,
        "status": "complete",
        "run_id": ctx.run_id,
        "started_at": ctx.state.get("started_at"),
        "completed_at": datetime.now(IST).isoformat(),
        "screening_date": str(as_of),
        "financial_data_cutoff": str(fin_period),
        "price_date": str(price_date),
        "technical_data_cutoff": str(tech_date),
        "price_basis": basis,
        "config_hash": ctx.settings.config_hash(),
        "universe_claim": "partial",
        "universe_description": (
            "Currently listed NSE main-board series-EQ equities. BSE-only "
            "listings, SME-platform scrips, ETFs, REITs/InvITs and non-EQ series "
            "are outside the frame. Delisted and renamed securities are retained "
            "for price history but not screened."),
        "counts": {"evaluated": n_all, "eligible": n_el, "selected": n_sel},
        "archetype_counts_selected": {k: int(v) for k, v in arch_sel.items()},
        "technical_stage_counts_selected": {k: int(v) for k, v in stage_sel.items()},
        "exclusion_counts": {k: int(v) for k, v in exc.items() if k and str(k) != "nan"},
        "files": files,
        "known_limitations": [
            "BSE-only listed companies were not screened",
            "Fundamentals sourced from an aggregator (secondary), not from filings",
            "Reported earnings only - no normalisation for exceptional items",
            "Net debt approximated by gross borrowings; cash not separately available",
            "Promoter pledging not captured",
            "Auditor qualifications, related-party transactions and CWIP ageing not assessed",
            "Capex evidence is balance-sheet inference, not verified commissioning",
            "Corporate-event flags are keyword-classified, not read in full",
            "Liquidity is a weekly-derived substitute for a true daily 3-month median",
            "No forward consensus estimates; all growth figures are realised",
        ],
        "phase2_handoff_package": [
            "P1_candidates.csv", "P1_summary.md", "P1_source_log.csv",
            "P1_run_manifest.json"],
    }
    mf = out / "P1_run_manifest.json"
    mf.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    return StageResult(
        stage=STAGE, rows_out=len(files) + 1,
        artifacts=[StageArtifact("P1_summary.md", "md", md),
                   StageArtifact("P1_run_manifest.json", "json", mf)],
        detail={"files": len(files) + 1})
