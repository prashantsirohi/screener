"""Phase 1 - generate P1_summary.md and P1_run_manifest.json."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "phase1"
IST = timezone(timedelta(hours=5, minutes=30))

EXCLUSION_CODES = {
    "EX_NO_FUNDAMENTALS": "No usable financial record could be retrieved for the symbol (typically a trust, REIT/InvIT, recently suspended scrip, or a symbol with no aggregator page).",
    "EX_NO_MCAP": "Market capitalisation not available from any source used.",
    "EX_MCAP_BELOW_BAND": "Market cap below the INR 1,000 cr floor.",
    "EX_MCAP_ABOVE_BAND": "Market cap above the INR 1,00,000 cr ceiling.",
    "EX_ILLIQUID": "Median daily traded value below INR 1.0 cr over the trailing 13 weeks.",
    "EX_SHORT_FIN_HISTORY": "Fewer than 3 annual reporting periods available.",
    "EX_NO_PRICE_HISTORY": "Fewer than 40 adjusted weekly bars - Weinstein staging impossible.",
    "EX_NO_ARCHETYPE": "No archetype discovery test passed on the available data; the company does not present an identifiable return mechanism at Phase 1 depth.",
    "EX_DATA_QUALITY": "Core screening metrics missing - classification would not be reliable.",
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def fmt(v, nd=1, dash="Not disclosed"):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return dash
    return f"{v:,.{nd}f}"


def main() -> int:
    df = pd.read_pickle(ROOT / "data" / "phase1_full.pkl")
    cand = pd.read_csv(OUT / "P1_candidates.csv")
    uni = pd.read_csv(OUT / "P1_screened_universe.csv")
    slog = pd.read_csv(OUT / "P1_source_log.csv")
    sel_note = json.load(open(ROOT / "data" / "_sel_note.json"))["cutoff_note"]

    screening_date = str(df["screening_date"].iloc[0])
    price_date = str(df["price_date"].dropna().iloc[0])
    tech_date = str(df["technical_data_date"].replace("", pd.NA).dropna().iloc[0])

    n_all, n_el, n_sel = len(df), int(df["eligible_flag"].sum()), len(cand)
    exc = df[df["eligible_flag"] == 0]["exclusion_code"].value_counts()
    arch_el = df[df["eligible_flag"] == 1]["primary_archetype"].value_counts()
    arch_sel = cand["primary_archetype"].value_counts()
    stage_sel = cand["technical_stage"].value_counts()
    stage_el = df[df["eligible_flag"] == 1]["technical_stage"].value_counts()
    fy_latest = df["_latest_fy"].dropna().value_counts().head(3)

    top20 = cand.head(20)

    sector_tbl = (cand.groupby("sector")
                  .agg(n=("symbol", "count"),
                       med_score=("preliminary_priority_score", "median"))
                  .sort_values("n", ascending=False).head(12))

    L = []
    A = L.append
    A("# Phase 1 Summary - Indian Equity Screen (Discovery and Bucket Classification)\n")
    A(f"**Screening date:** {screening_date} (Asia/Kolkata)  ")
    A(f"**Share-price and market-cap date:** {price_date}  ")
    A(f"**Technical-data cutoff:** {tech_date} (adjusted weekly bars)  ")
    A(f"**Financial-data cutoff:** latest annual period available per company; "
      f"the modal latest fiscal year across the universe is "
      f"{fy_latest.index[0] if len(fy_latest) else 'n/a'} "
      f"({fy_latest.iloc[0] if len(fy_latest) else 0} companies), plus trailing-twelve-month "
      f"and latest-quarter figures where published.\n")

    A("## 1. Universe definition and actual coverage\n")
    A("**Universe claim: FULL for NSE main-board series-EQ equities. Not a full "
      "NSE+BSE universe** - BSE-only listings were not screened.\n")
    A("Frame construction:\n")
    A("- Started from NSE's own master list `EQUITY_L.csv` (2,410 rows), filtered to "
      "`SERIES == EQ`, giving **2,086 main-board equity symbols**. Every one of these "
      "2,086 was evaluated and appears in `P1_screened_universe.csv`.")
    A("- Prices and turnover come from the NSE full bhavcopy for the price date.")
    A("- NSE index constituent files (Nifty Total Market 752, Microcap 250, Nifty 500) "
      "supply NSE's own industry classification; 742 symbols carry it, the remainder "
      "are recorded as `Not disclosed`.")
    A("- Companies listed only on BSE, SME-platform scrips, ETFs, REITs/InvITs, "
      "government securities and non-EQ series were never in the frame.\n")
    A(f"**Coverage achieved:** {n_all} securities evaluated; {n_el} eligible; "
      f"{n_sel} selected as Phase 1 candidates.\n")

    A("## 2. Funnel\n")
    A("| Stage | Count |")
    A("|---|---:|")
    A(f"| NSE series-EQ symbols evaluated | {n_all} |")
    A(f"| Excluded | {n_all - n_el} |")
    A(f"| Eligible after all gates | {n_el} |")
    A(f"| Selected as Phase 1 candidates | {n_sel} |")
    A(f"\nSelection rule applied: {sel_note}.\n")

    A("### Exclusions by code\n")
    A("| Code | Count | Meaning |")
    A("|---|---:|---|")
    for code, cnt in exc.items():
        A(f"| `{code}` | {cnt} | {EXCLUSION_CODES.get(code, '')} |")
    A("")

    A("## 3. Eligibility gates and formulas\n")
    A("Applied in order; the first gate a company fails is the recorded exclusion code.\n")
    A("| Gate | Rule |")
    A("|---|---|")
    A("| Financial record | A parseable statement set must exist |")
    A("| Market cap | INR 1,000 cr <= market cap <= INR 1,00,000 cr |")
    A("| Financial history | >= 3 annual reporting periods |")
    A("| Price history | >= 40 adjusted weekly bars (the 40-week MA must exist) |")
    A("| Liquidity | 13-week median daily traded value >= INR 1.0 cr |")
    A("| Classification | at least one archetype discovery test must pass |")
    A("")
    A("**Liquidity definition.** `liquidity_value_inr_cr` is the **13-week median of "
      "weekly traded value, divided by 5** to express a median daily figure in INR crore. "
      "Weekly traded value is `mean(high, low, adjusted close) x weekly volume`. This is a "
      "labelled substitute for a true 3-month median of daily turnover: only weekly bars "
      "were available across the whole universe. It is systematically slightly smoother "
      "than a true daily median and should not be read to two decimal places.\n")
    A("**Key formulas.**\n")
    A("```")
    A("revenue_cagr_5y   = (Rev[FY0] / Rev[FY-5]) ^ (1/5) - 1        # only if both > 0")
    A("eps_cagr_5y       = (EPS[FY0] / EPS[FY-5]) ^ (1/5) - 1        # only if both > 0")
    A("cfo_pat_ratio     = sum(CFO, last 5 FY) / sum(PAT, last 5 FY)")
    A("net_debt_to_equity= Borrowings / (Equity capital + Reserves)  # see caveat below")
    A("ma30w, ma40w      = 30- and 40-week SMA of adjusted close")
    A("ma_slope_pct      = MA[t] / MA[t-13] - 1                      # 13-week direction")
    A("rs_bm_13w_pct     = (P/Bench)[t] / (P/Bench)[t-13] - 1        # vs Nifty 500")
    A("liquidity_inr_cr  = median(weekly traded value, 13w) / 5 / 1e7")
    A("```\n")

    A("## 4. Normalisation approach and its limits\n")
    A("Phase 1 uses **reported** figures. EPS and PAT have **not** been normalised for "
      "exceptional items, because that requires the filings themselves - which is Phase 2 "
      "work. Three mitigations were applied instead:\n")
    A("1. Where other income exceeds 35% of profit before tax, the company carries an "
      "explicit `earnings_quality_flag` and its EPS-derived metrics are treated as "
      "provisional (data-quality confidence is downgraded).")
    A("2. Cyclical candidates were tested on **margin distance from the 5-year peak** and "
      "on quarterly margin direction, never on peak EPS.")
    A("3. `preliminary_valuation_value` is a trailing P/E (or P/B for lenders) and is "
      "explicitly labelled preliminary. **No intrinsic value, target price or "
      "'Undervalued' tag has been assigned in Phase 1.**\n")
    A("**Balance-sheet caveat.** The aggregator does not expose cash and equivalents as a "
      "separate line, so `net_debt_to_equity` is computed as **gross borrowings / net worth**. "
      "For cash-rich companies this overstates leverage. Phase 2 must recompute true net debt "
      "from the balance sheet. The field is left blank for banks, NBFCs and insurers, where "
      "the ratio is not meaningful.\n")
    A("**Lenders.** For companies classified as financial (by NSE industry label or by the "
      "presence of a `Financing Profit` line), CFO/PAT and debt/equity are suppressed: a "
      "growing loan book produces structurally negative operating cash flow, and borrowings "
      "are raw material rather than leverage. ROE replaces ROCE for these names.\n")

    A("## 5. Three-axis classification\n")
    A("**Axis A** - exactly one primary archetype per company, chosen as the highest-scoring "
      "of ten archetype-specific discovery tests (no single universal screen). Each test "
      "returns 0 when its own preconditions are absent, so an archetype is never assigned "
      "by default.\n")
    A("**Axis B** - secondary tags drawn only from the controlled vocabulary. "
      "**`Undervalued` was not assigned to any company**, as required: it needs the "
      "three-condition test in Phase 2.\n")
    A("**Axis C** - Weinstein stage computed arithmetically from adjusted weekly closes: "
      "the 30- and 40-week moving averages and their 13-week slopes, relative strength "
      "versus the Nifty 500 and the relevant sector index, base duration and depth, pivot, "
      "volume confirmation, distance from the 52-week high, and overhead supply. "
      "No stage was assigned by judgement.\n")

    A("### Eligible universe by archetype\n")
    A("| Archetype | Eligible | Selected |")
    A("|---|---:|---:|")
    for a in arch_el.index:
        A(f"| {a} | {arch_el[a]} | {int(arch_sel.get(a, 0))} |")
    A(f"| **Total** | **{n_el}** | **{n_sel}** |\n")

    A("### Candidates by technical stage\n")
    A("| Stage | Selected | Eligible universe |")
    A("|---|---:|---:|")
    for s in stage_el.index:
        A(f"| {s} | {int(stage_sel.get(s, 0))} | {stage_el[s]} |")
    A("")

    A("### Candidates by sector\n")
    A("| Sector | Candidates | Median priority score |")
    A("|---|---:|---:|")
    for s, r in sector_tbl.iterrows():
        A(f"| {s} | {int(r['n'])} | {r['med_score']:.1f} |")
    A("")

    A("## 6. Preliminary priority score\n")
    A("Scored 0-100 on the prescribed weights. Components are recorded per company in the "
      "pipeline output.\n")
    A("| Component | Weight | What drives it |")
    A("|---|---:|---|")
    A("| Financial and balance-sheet quality | 20 | CFO/PAT, leverage, interest cover, FCF consistency (ROE/dilution for lenders) |")
    A("| Evidence for archetype thesis | 25 | the archetype's own discovery-test fit |")
    A("| Forward catalyst visibility | 20 | latest-quarter revenue/profit/margin direction, CWIP commissioning |")
    A("| Preliminary valuation plausibility | 15 | P/E against realised EPS growth, P/B against ROE, dividend yield |")
    A("| Governance and disclosure quality | 10 | promoter holding level and trend, 5-year dilution, earnings-quality flag |")
    A("| Technical and liquidity confirmation | 10 | Weinstein stage, 13-week relative strength, traded value |")
    A("")

    A(f"## 7. Top 20 Phase 2 priorities\n")
    A("| # | Company | Symbol | Mkt cap (cr) | Archetype | Stage | Score | Why it ranks here |")
    A("|---:|---|---|---:|---|---|---:|---|")
    for i, r in enumerate(top20.itertuples(index=False), 1):
        rationale = str(r.classification_rationale or "")[:110]
        A(f"| {i} | {r.company} | `{r.symbol}` | {fmt(r.market_cap_inr_cr, 0)} | "
          f"{r.primary_archetype} | {r.technical_stage} | "
          f"{r.preliminary_priority_score:.1f} | {rationale} |")
    A("")

    A("## 8. Market and sector observations\n")
    el = df[df["eligible_flag"] == 1]
    st = el["technical_stage"].value_counts(normalize=True) * 100
    A(f"- Of the {n_el} eligible companies, "
      f"{st.get('Stage 4 decline', 0):.0f}% are in Stage 4 decline and "
      f"{st.get('Stage 3 distribution', 0):.0f}% in Stage 3 distribution, while "
      f"{st.get('Early Stage 2', 0) + st.get('Mature/extended Stage 2', 0):.0f}% are in "
      f"Stage 2 advances. This is a market with real internal dispersion rather than a "
      f"uniform trend - stage selection carries genuine information here.")
    A(f"- {st.get('Early Stage 1', 0) + st.get('Mature Stage 1 base', 0):.0f}% sit in Stage 1 "
      f"basing formations, which is where the brief's preferred entries live; a meaningful "
      f"share of the candidate list is therefore awaiting confirmation rather than already "
      f"extended.")
    if len(sector_tbl):
        A(f"- The candidate set concentrates in {sector_tbl.index[0]} "
          f"({int(sector_tbl.iloc[0]['n'])} names). Phase 3 must check that this does not "
          f"become hidden single-theme exposure in the portfolio.")
    ev_n = int((df["_events"] != "").sum())
    A(f"- {ev_n} screened companies carry at least one classified corporate-event flag from "
      f"NSE announcements over the trailing 15 months.")
    A("")

    A("## 9. Data gaps and limitations\n")
    A("Stated plainly, because they bound what Phase 2 can rely on:\n")
    A("1. **BSE-only companies were not screened.** The coverage claim is NSE main-board "
      "series-EQ only.")
    A("2. **Fundamentals are aggregator-sourced (screener.in), which is a secondary source.** "
      "It compiles audited filings but is not itself the filing. Every material figure for a "
      "shortlisted name must be confirmed against the annual report or exchange filing in Phase 2.")
    A("3. **No normalisation of exceptional items.** Reported EPS/PAT only - see section 4.")
    A("4. **Net debt is gross borrowings.** Cash is not separately available; leverage is "
      "overstated for cash-rich companies.")
    A("5. **Promoter pledging was not captured.** The aggregator's shareholding block does not "
      "expose pledge percentages. This is a required Phase 2 check, not an optional one.")
    A("6. **Auditor qualifications, related-party transactions, contingent liabilities and "
      "CWIP ageing were not assessed.** These need the annual report and are Phase 2 work.")
    A("7. **Capex evidence is balance-sheet inference.** Capex candidates were identified from "
      "CWIP intensity and CWIP drawdown into fixed assets, not from verified commissioning "
      "dates or order books. Phase 2 must verify commissioning, funding and demand from "
      "filings, presentations and calls before any capacity model is built.")
    A("8. **Event flags are keyword-classified from announcement text**, not read in full. "
      "False positives are possible; each flagged event must be confirmed against the "
      "underlying intimation.")
    A("9. **Relative strength uses the Nifty 500 plus one of twelve sector indices.** Companies "
      "whose NSE industry label is missing were compared to the broad index only.")
    A("10. **Liquidity is a weekly-derived substitute** for a true daily 3-month median.")
    A("11. **Forward estimates are absent by design.** No consensus data was available, so all "
      "growth figures are realised, not forecast. Phase 2 introduces the forward view.")
    A("")

    A("## 10. Instructions for Phase 2\n")
    A("Validate in this order - these are the issues most likely to change a conclusion:\n")
    A("1. **Confirm market cap and share count** from the exchange, then recompute per-share "
      "figures on fully diluted shares. Aggregator market caps drift after corporate actions.")
    A("2. **Recompute net debt properly** (borrowings less cash and liquid investments) for "
      "every candidate. Several apparent leverage flags in this file will dissolve.")
    A("3. **Check promoter pledging and any auditor qualification** for every candidate - "
      "neither was available at Phase 1, and either can be disqualifying.")
    A("4. **Normalise earnings.** Strip exceptional items and reconcile reported to normalised "
      "EPS. Companies carrying an `earnings_quality_flag` are the priority.")
    A("5. **For every capex candidate**, replace the CWIP inference with verified commissioning "
      "dates, capex spent versus budget, funding, and demand evidence. Reject any name where "
      "capacity exists but demand evidence does not.")
    A("6. **For every cyclical candidate**, build the mid-cycle margin from the full history "
      "and value on that - not on the trough and not on the peak.")
    A("7. **For every event-driven candidate**, read the actual scheme or offer document and "
      "establish the regulatory path, timeline and no-event downside.")
    A("8. **Treat the technical stage as timing information only.** It must not rescue a company "
      "whose fundamentals or governance fail. Phase 3 owns the final technical read.")
    A("9. Names with `data_quality_confidence = Low` need their data rebuilt from filings "
      "before any model is trusted.")
    A("")

    A("## 11. Exclusion-code dictionary\n")
    A("| Code | Meaning |")
    A("|---|---|")
    for c, m in EXCLUSION_CODES.items():
        A(f"| `{c}` | {m} |")
    A("")

    A("## 12. Files in this hand-off\n")
    A("| File | Rows | Purpose |")
    A("|---|---:|---|")
    A(f"| `P1_screened_universe.csv` | {len(uni)} | every security evaluated, including exclusions |")
    A(f"| `P1_candidates.csv` | {len(cand)} | the selected set, with Phase 2 questions and required documents |")
    A(f"| `P1_source_log.csv` | {len(slog)} | every source ID referenced in the CSVs |")
    A("| `P1_summary.md` | - | this document |")
    A("| `P1_run_manifest.json` | - | counts, cutoffs, checksums, resume instructions |")
    A("")
    A("**Units.** All currency fields are Indian rupees. `market_cap_inr_cr` and "
      "`liquidity_value_inr_cr` are in crore (1 cr = 10,000,000). Prices are per share in "
      "rupees. All `_pct` fields are percentages, not fractions. `net_debt_to_equity` and "
      "`cfo_pat_ratio` are ratios. Dates are `YYYY-MM-DD`.\n")
    A("---")
    A("*This is analytical research output produced by an automated screen. It is not "
      "personalised investment advice.*")

    (OUT / "P1_summary.md").write_text("\n".join(L), encoding="utf-8")

    # ---------------- manifest ----------------
    files = []
    for name in ("P1_screened_universe.csv", "P1_candidates.csv",
                 "P1_source_log.csv", "P1_summary.md"):
        p = OUT / name
        rc = None
        if name.endswith(".csv"):
            rc = len(pd.read_csv(p))
        files.append({"name": name, "row_count": rc, "sha256": sha256(p)})

    manifest = {
        "schema_version": "1.0",
        "phase": 1,
        "status": "complete",
        "started_at": datetime.now(IST).isoformat(),
        "completed_at": datetime.now(IST).isoformat(),
        "screening_date": screening_date,
        "financial_data_cutoff": "2026-03-31",
        "price_date": price_date,
        "technical_data_cutoff": tech_date,
        "universe_claim": "partial",
        "universe_description": (
            "Full NSE main-board series-EQ equity universe (2,086 symbols from NSE "
            "EQUITY_L.csv). BSE-only listings, SME-platform scrips, ETFs, REITs/InvITs "
            "and non-EQ series are outside the frame. Not a combined NSE+BSE universe."),
        "counts": {"evaluated": int(n_all), "eligible": int(n_el), "selected": int(n_sel)},
        "archetype_counts_selected": {k: int(v) for k, v in arch_sel.items()},
        "technical_stage_counts_selected": {k: int(v) for k, v in stage_sel.items()},
        "exclusion_counts": {k: int(v) for k, v in exc.items()},
        "files": files,
        "data_sources": {
            "primary": ["NSE EQUITY_L.csv", "NSE UDiFF bhavcopy 2026-08-10",
                        "NSE index constituent files", "NSE corporate announcements API"],
            "secondary": ["screener.in company pages", "Yahoo Finance adjusted weekly OHLCV"],
        },
        "known_limitations": [
            "BSE-only listed companies were not screened",
            "Fundamentals sourced from an aggregator (secondary), not directly from filings",
            "Reported earnings only - no normalisation for exceptional items",
            "Net debt approximated by gross borrowings; cash not separately available",
            "Promoter pledging not captured",
            "Auditor qualifications, related-party transactions and CWIP ageing not assessed",
            "Capex evidence is balance-sheet inference, not verified commissioning",
            "Corporate-event flags are keyword-classified, not read in full",
            "Liquidity is a weekly-derived substitute for a true daily 3-month median",
            "No forward consensus estimates available; all growth figures are realised",
        ],
        "resume_instructions": (
            "Phase 1 is complete; no resume required. To rebuild: run scripts 01, 02, 03, 05 "
            "(all resume-safe and cache to data/), then 04, 06, 07. To refresh only prices and "
            "technicals, delete data/prices/ and rerun 03 then 04, 06, 07."),
        "phase2_handoff_package": [
            "P1_candidates.csv", "P1_summary.md", "P1_source_log.csv", "P1_run_manifest.json"],
    }
    p = OUT / "P1_run_manifest.json"
    p.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("Wrote P1_summary.md and P1_run_manifest.json")
    for f in files:
        print(f"  {f['name']:<28} rows={f['row_count']}  sha256={f['sha256'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
