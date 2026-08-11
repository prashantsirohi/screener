"""
Phase 1 - emit the five hand-off artefacts from the screened frame.

Reads data/phase1_full.pkl (written by 04_phase1_screen.py) and produces:
    phase1/P1_screened_universe.csv
    phase1/P1_candidates.csv
    phase1/P1_source_log.csv
    phase1/P1_summary.md
    phase1/P1_run_manifest.json
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "phase1"
OUT.mkdir(parents=True, exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))

SCREEN_COLS = [
    "screening_date", "company", "symbol", "exchange", "listing_board", "sector", "industry",
    "current_price_inr", "price_date", "market_cap_inr_cr", "market_cap_date",
    "liquidity_metric_name", "liquidity_value_inr_cr", "liquidity_period",
    "eligible_flag", "exclusion_code", "exclusion_reason",
    "primary_archetype", "classification_rationale", "secondary_tags",
    "technical_stage", "technical_data_date",
    "revenue_cagr_5y_pct", "normalized_eps_cagr_5y_pct",
    "latest_roce_or_roe_pct", "median_roce_or_roe_5y_pct", "net_debt_to_equity",
    "cfo_pat_period", "cfo_pat_ratio",
    "preliminary_valuation_method", "preliminary_valuation_value",
    "preliminary_priority_score", "inclusion_reason", "key_disqualifying_risk",
    "data_quality_confidence", "primary_source_ids", "secondary_source_ids",
]

TARGET_LO, TARGET_HI = 100, 150
MIN_SELECT_SCORE = 45.0

ARCHETYPE_QUESTIONS = {
    "Quality compounder": [
        "Is the 5y median ROCE sustained on incremental capital, or inflated by a legacy asset base?",
        "What is the reinvestment runway, and has capital allocation stayed inside the core?",
        "Does cumulative CFO/PAT hold above 0.8 once working-capital swings are normalised?",
    ],
    "High-growth company": [
        "Is forward growth contracted/visible or extrapolated from a favourable base?",
        "Do unit economics hold as mix shifts, and is growth funded without dilution?",
        "How much of EPS growth is operating vs. below-the-line or tax-rate driven?",
    ],
    "Capex operating-leverage candidate": [
        "What exactly has been commissioned, on what date, and at what capex vs. original budget?",
        "What demand evidence exists (orders, nominations, approvals, contracts) for the new capacity?",
        "What are incremental EBITDA margin and incremental ROCE at 50/70/85% utilisation?",
        "How do 6/12/18-month commissioning delays change FY+2 EPS and the debt schedule?",
    ],
    "Cyclical recovery": [
        "Where is the cycle versus mid-cycle margin, and what capacity is still being added industry-wide?",
        "Is the improvement demand-led or a one-off inventory/input-cost effect?",
        "Does the balance sheet survive another 4-6 quarters at trough spreads?",
    ],
    "Turnaround": [
        "What concretely changed - management, business model, cost base, or only the narrative?",
        "What is the funding runway to EBITDA breakeven and what must be refinanced?",
        "Is working capital genuinely releasing cash, or are receivables/inventory masking it?",
    ],
    "Asset-value/SOTP opportunity": [
        "What are conservative realisable values for each asset, net of tax leakage and holdco discount?",
        "Is there a disclosed unlocking mechanism with a timeline, or only a static discount?",
        "Does the core operating business earn above its cost of capital, or does it consume the asset value?",
    ],
    "Financial compounder": [
        "Is ROE sustainable on current capital, and what dilution is required to fund growth?",
        "What are GNPA/NNPA, provision coverage, and credit-cost trend through the cycle?",
        "What is the funding franchise - cost of funds trajectory and liability mix?",
    ],
    "Mature value/yield company": [
        "Is the dividend covered by normalised FCF after maintenance capex, not by peak earnings?",
        "What structural-decline risk exists in the core end-market?",
        "Is management willing to keep returning capital, on what stated policy?",
    ],
    "Event-driven or special situation": [
        "What is the disclosed scheme/transaction structure, record date, and regulatory path?",
        "What is the value in completed, delayed and cancelled scenarios, and the probability of each?",
        "What is the downside if the event does not complete at all?",
    ],
    "Speculative/emerging business": [
        "What is the cash runway in quarters, and what dilution closes the gap to breakeven?",
        "Are unit economics positive at the contribution level today?",
        "What explicit failure probability and residual value should be modelled?",
    ],
}

ARCHETYPE_DOCS = {
    "Quality compounder": "Latest annual report (MD&A + segment note); last 4 quarterly results; latest investor presentation; last 2 earnings-call transcripts",
    "High-growth company": "Last 4 quarterly results; latest investor presentation; earnings-call transcripts; annual report share-capital and ESOP notes",
    "Capex operating-leverage candidate": "Capex announcements and Reg-30 intimations; annual report CWIP/fixed-asset schedule; investor presentation capacity slides; earnings-call transcripts; credit-rating rationale",
    "Cyclical recovery": "Annual report segment/realisation data; quarterly results; industry capacity data; credit-rating rationale",
    "Turnaround": "Annual report auditor's report and going-concern note; debt-restructuring disclosures; quarterly results; credit-rating rationale",
    "Asset-value/SOTP opportunity": "Annual report investment schedule and subsidiary financials; scheme documents; related-party transaction note",
    "Financial compounder": "Annual report asset-quality and capital-adequacy disclosures; quarterly investor presentation; credit-rating rationale",
    "Mature value/yield company": "Annual report cash-flow statement and dividend policy; quarterly results; capex guidance",
    "Event-driven or special situation": "Scheme of arrangement / offer document; stock-exchange intimations; NCLT/SEBI filings; valuation report where disclosed",
    "Speculative/emerging business": "Annual report going-concern and cash-flow notes; fund-raise disclosures; quarterly results",
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def gaps_for(r) -> str:
    g = []
    if pd.isna(r.get("revenue_cagr_5y_pct")):
        g.append("5y revenue CAGR not computable (short/loss-making history)")
    if pd.isna(r.get("normalized_eps_cagr_5y_pct")):
        g.append("5y EPS CAGR not computable (negative or zero base year)")
    if pd.isna(r.get("cfo_pat_ratio")) and not r.get("_is_financial"):
        g.append("cumulative CFO/PAT unavailable")
    if pd.isna(r.get("median_roce_or_roe_5y_pct")):
        g.append("5y median return metric unavailable")
    if r.get("_eq_flag"):
        g.append(str(r["_eq_flag"]))
    g.append("EPS not normalised for exceptional items at Phase 1 - reported basis only")
    if r.get("_basis") == "standalone":
        g.append("standalone basis only - no consolidated statements on the aggregator")
    return "; ".join(g)


def main() -> int:
    df = pd.read_pickle(ROOT / "data" / "phase1_full.pkl")
    screening_date = df["screening_date"].iloc[0]

    # ---------------- selection ----------------
    el = df[df["eligible_flag"] == 1].copy()
    el = el.sort_values("preliminary_priority_score", ascending=False)
    pool = el[el["preliminary_priority_score"] >= MIN_SELECT_SCORE]
    if len(pool) > TARGET_HI:
        cand = pool.head(TARGET_HI).copy()
        cutoff_note = (f"top {TARGET_HI} by preliminary priority score "
                       f"(score floor {cand['preliminary_priority_score'].min():.1f})")
    else:
        cand = pool.copy()
        cutoff_note = (f"all eligible names scoring >= {MIN_SELECT_SCORE} "
                       f"({len(cand)} names)")

    # phase2_priority by tertile within the selected set
    if len(cand):
        q1, q2 = cand["preliminary_priority_score"].quantile([1 / 3, 2 / 3])
        cand["phase2_priority"] = cand["preliminary_priority_score"].apply(
            lambda s: "High" if s >= q2 else ("Medium" if s >= q1 else "Low"))
        cand["phase2_questions"] = cand["primary_archetype"].map(
            lambda a: " | ".join(ARCHETYPE_QUESTIONS.get(a, [])))
        cand["required_primary_documents"] = cand["primary_archetype"].map(
            lambda a: ARCHETYPE_DOCS.get(a, "Annual report; quarterly results; investor presentation"))
        cand["known_data_gaps"] = cand.apply(gaps_for, axis=1)
        cand = cand.sort_values(["preliminary_priority_score", "primary_archetype"],
                                ascending=[False, True])

    # ---------------- write CSVs ----------------
    uni_out = df[SCREEN_COLS].copy()
    uni_out.to_csv(OUT / "P1_screened_universe.csv", index=False, encoding="utf-8")

    cand_out = cand[SCREEN_COLS + ["phase2_priority", "phase2_questions",
                                   "required_primary_documents", "known_data_gaps"]].copy()
    cand_out.to_csv(OUT / "P1_candidates.csv", index=False, encoding="utf-8")

    # ---------------- source log ----------------
    acc = screening_date
    src = [
        {"source_id": "NSE-EQL-2026-08-10", "company": "ALL", "symbol": "ALL",
         "document_type": "Exchange master list", "title": "EQUITY_L.csv - securities available for trading",
         "issuer": "National Stock Exchange of India", "published_date": acc,
         "period_covered": acc, "url": "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
         "primary_or_secondary": "Primary", "accessed_date": acc,
         "claim_or_metric_supported": "Universe definition; listed symbols, ISIN, listing date, face value",
         "notes": "2,410 rows; 2,086 with series EQ"},
        {"source_id": "NSE-BHAV-2026-08-10", "company": "ALL", "symbol": "ALL",
         "document_type": "Exchange bhavcopy", "title": "BhavCopy_NSE_CM full UDiFF bhavcopy 2026-08-10",
         "issuer": "National Stock Exchange of India", "published_date": acc,
         "period_covered": acc,
         "url": "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260810_F_0000.csv.zip",
         "primary_or_secondary": "Primary", "accessed_date": acc,
         "claim_or_metric_supported": "Traded close price and turnover on the price date",
         "notes": "2,428 series-EQ rows"},
        {"source_id": "NSE-IDX-TOTALMKT-2026-08-10", "company": "ALL", "symbol": "ALL",
         "document_type": "Index constituent list", "title": "Nifty Total Market index constituents",
         "issuer": "NSE Indices", "published_date": acc, "period_covered": acc,
         "url": "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv",
         "primary_or_secondary": "Primary", "accessed_date": acc,
         "claim_or_metric_supported": "Industry/sector classification", "notes": "752 constituents"},
        {"source_id": "NSE-IDX-MICRO250-2026-08-10", "company": "ALL", "symbol": "ALL",
         "document_type": "Index constituent list", "title": "Nifty Microcap 250 index constituents",
         "issuer": "NSE Indices", "published_date": acc, "period_covered": acc,
         "url": "https://nsearchives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv",
         "primary_or_secondary": "Primary", "accessed_date": acc,
         "claim_or_metric_supported": "Industry/sector classification", "notes": "252 constituents"},
        {"source_id": "NSE-IDX-N500-2026-08-10", "company": "ALL", "symbol": "ALL",
         "document_type": "Index constituent list", "title": "Nifty 500 index constituents",
         "issuer": "NSE Indices", "published_date": acc, "period_covered": acc,
         "url": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
         "primary_or_secondary": "Primary", "accessed_date": acc,
         "claim_or_metric_supported": "Industry/sector classification", "notes": "500 constituents"},
        {"source_id": "NSE-ANN-2025-05-2026-08", "company": "ALL", "symbol": "ALL",
         "document_type": "Corporate announcements", "title": "NSE corporate announcements (equities)",
         "issuer": "National Stock Exchange of India", "published_date": acc,
         "period_covered": "2025-05-17 to 2026-08-10",
         "url": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
         "primary_or_secondary": "Primary", "accessed_date": acc,
         "claim_or_metric_supported": "Event-driven archetype; demerger/open-offer/asset-sale/governance flags",
         "notes": "233,118 announcements parsed; 1,556 event flags across 1,002 symbols"},
        {"source_id": "YF-BENCH-N500", "company": "Nifty 500 index", "symbol": "^CRSLDX",
         "document_type": "Market data", "title": "Nifty 500 adjusted weekly series",
         "issuer": "Yahoo Finance", "published_date": acc, "period_covered": "2021-08 to 2026-08",
         "url": "https://query1.finance.yahoo.com/v8/finance/chart/%5ECRSLDX?range=5y&interval=1wk",
         "primary_or_secondary": "Secondary", "accessed_date": acc,
         "claim_or_metric_supported": "Relative-strength benchmark for Weinstein stage analysis",
         "notes": "262 weekly bars"},
    ]
    for r in df.itertuples(index=False):
        sym = r.symbol
        if r.eligible_flag == 1 or r.exclusion_code in ("EX_MCAP_ABOVE_BAND", "EX_MCAP_BELOW_BAND"):
            src.append({
                "source_id": f"{sym}-SCR-01", "company": r.company, "symbol": sym,
                "document_type": "Aggregated financial statements",
                "title": f"screener.in company page ({r._asdict().get('_basis') or 'n/a'} basis)",
                "issuer": "screener.in", "published_date": acc,
                "period_covered": str(r._asdict().get("_latest_fy") or ""),
                "url": r._asdict().get("_src_url") or f"https://www.screener.in/company/{sym}/",
                "primary_or_secondary": "Secondary", "accessed_date": acc,
                "claim_or_metric_supported": "Annual/quarterly P&L, balance sheet, cash flow, ratios, shareholding",
                "notes": "Aggregator of audited filings - material claims to be confirmed against filings in Phase 2",
            })
            src.append({
                "source_id": f"{sym}-PXW-01", "company": r.company, "symbol": sym,
                "document_type": "Market data",
                "title": f"{sym}.NS split/dividend-adjusted weekly OHLCV",
                "issuer": "Yahoo Finance", "published_date": acc,
                "period_covered": "2021-08 to 2026-08",
                "url": f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?range=5y&interval=1wk",
                "primary_or_secondary": "Secondary", "accessed_date": acc,
                "claim_or_metric_supported": "Weinstein stage, 30/40w MA, relative strength, base, volume, liquidity",
                "notes": "adjclose used for all stage arithmetic",
            })
            evs = r._asdict().get("_events")
            if evs:
                src.append({
                    "source_id": f"{sym}-NSEANN-01", "company": r.company, "symbol": sym,
                    "document_type": "Corporate announcement",
                    "title": f"NSE announcement(s): {evs}",
                    "issuer": "National Stock Exchange of India",
                    "published_date": r._asdict().get("_event_latest") or acc,
                    "period_covered": "2025-05-17 to 2026-08-10",
                    "url": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
                    "primary_or_secondary": "Primary", "accessed_date": acc,
                    "claim_or_metric_supported": "Event-driven classification and/or governance flag",
                    "notes": "keyword-classified from announcement text; confirm the filing itself in Phase 2",
                })
    slog = pd.DataFrame(src).drop_duplicates("source_id")
    slog.to_csv(OUT / "P1_source_log.csv", index=False, encoding="utf-8")

    print(f"universe rows : {len(uni_out)}")
    print(f"candidates    : {len(cand_out)}  ({cutoff_note})")
    print(f"source log    : {len(slog)}")
    json.dump({"cutoff_note": cutoff_note}, open(ROOT / "data" / "_sel_note.json", "w"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
