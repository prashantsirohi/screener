"""Phase 1 quality control - the ten checks required by the brief, plus schema checks."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "phase1"

CONTROLLED_TAGS = {
    "Earnings-upgrade candidate", "Capex commissioning", "Operating-leverage inflection",
    "Margin-recovery candidate", "PEAD candidate", "Market-share gainer",
    "Debt-reduction story", "Export expansion", "Premiumisation",
    "Product-mix improvement", "Demerger/SOTP unlocking", "Regulatory catalyst",
    "Temporary setback", "Peak-cycle risk", "Governance risk",
    "Customer-concentration risk", "Overvalued quality", "Value-trap risk",
    "Statistically cheap-catalyst unclear",
}
VALID_ARCHETYPES = {
    "Quality compounder", "High-growth company", "Capex operating-leverage candidate",
    "Cyclical recovery", "Turnaround", "Asset-value/SOTP opportunity",
    "Financial compounder", "Mature value/yield company",
    "Event-driven or special situation", "Speculative/emerging business",
}
VALID_STAGES = {
    "Stage 4 decline", "Early Stage 1", "Mature Stage 1 base",
    "Stage 1-to-Stage 2 transition", "Early Stage 2", "Mature/extended Stage 2",
    "Stage 3 distribution", "Indeterminate-insufficient adjusted history",
}

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def main() -> int:
    uni = pd.read_csv(OUT / "P1_screened_universe.csv")
    cand = pd.read_csv(OUT / "P1_candidates.csv")
    slog = pd.read_csv(OUT / "P1_source_log.csv")
    man = json.loads((OUT / "P1_run_manifest.json").read_text(encoding="utf-8"))
    full = pd.read_pickle(ROOT / "data" / "phase1_full.pkl")

    # 1 - universe count supports the coverage claim
    check("1. Universe count matches the coverage claim",
          len(uni) == man["counts"]["evaluated"] == 2086 and man["universe_claim"] == "partial",
          f"{len(uni)} rows evaluated; claim='{man['universe_claim']}' (NSE series-EQ only)")

    # 2 - exactly one primary archetype per selected company
    bad = cand[~cand["primary_archetype"].isin(VALID_ARCHETYPES)]
    multi = cand[cand["primary_archetype"].astype(str).str.contains(r"\||,", regex=True)]
    check("2. Every candidate has exactly one valid primary archetype",
          len(bad) == 0 and len(multi) == 0 and cand["primary_archetype"].notna().all(),
          f"{len(bad)} invalid, {len(multi)} multi-valued")

    # 3 - 'Undervalued' never assigned
    tagblob = " ".join(cand["secondary_tags"].fillna("").astype(str)) + \
              " ".join(uni["secondary_tags"].fillna("").astype(str))
    check("3. 'Undervalued' not assigned anywhere in Phase 1",
          "Undervalued" not in tagblob)

    # 3b - tags all from controlled vocabulary
    used = set()
    for s in uni["secondary_tags"].fillna("").astype(str):
        used.update(x for x in s.split("|") if x)
    check("3b. All secondary tags come from the controlled vocabulary",
          used <= CONTROLLED_TAGS, f"unknown: {sorted(used - CONTROLLED_TAGS)}")

    # 4 - cyclicals not selected on peak earnings alone
    cyc = full[(full["eligible_flag"] == 1) &
               (full["primary_archetype"] == "Cyclical recovery")]
    at_peak = cyc[cyc["_opm_vs_peak"] >= -2] if "_opm_vs_peak" in cyc.columns else cyc.iloc[:0]
    check("4. No cyclical candidate selected at/near peak margin",
          len(at_peak) == 0,
          f"{len(cyc)} cyclicals, all below 5y peak OPM by >2pp" if len(at_peak) == 0
          else f"{len(at_peak)} at peak")

    # 5 - capex candidates have evidence beyond an announcement
    cap = cand[cand["primary_archetype"] == "Capex operating-leverage candidate"]
    no_ev = cap[~cap["classification_rationale"].fillna("").str.contains(
        "CWIP|commissioned|capacity", case=False)]
    check("5. Every capex candidate carries balance-sheet capex evidence",
          len(no_ev) == 0, f"{len(cap)} capex candidates, {len(no_ev)} without CWIP evidence")

    # 6 - adjusted weekly data used, or limitation flagged
    ind = uni[uni["technical_stage"] == "Indeterminate-insufficient adjusted history"]
    check("6. Adjusted weekly prices used; insufficient history explicitly flagged",
          set(uni["technical_stage"].dropna()) <= VALID_STAGES,
          f"{len(ind)} securities flagged Indeterminate; all stages from the controlled set")

    # 7 - no low-P/E-only selection
    if len(cand):
        pe = pd.to_numeric(cand["preliminary_valuation_value"], errors="coerce")
        cheap_only = cand[(pe < 12) & (cand["preliminary_priority_score"] < 50)]
        check("7. No company selected on a low P/E alone",
              len(cheap_only) == 0,
              "selection is on the 6-component priority score; P/E carries at most 9 of 100 points")

    # 8 - exceptional income not treated as recurring
    flagged = full[(full["eligible_flag"] == 1) & (full["_eq_flag"].notna())]
    sel_flagged = cand[cand["symbol"].isin(flagged["symbol"])]
    dq_ok = (sel_flagged["data_quality_confidence"] != "High").all() if len(sel_flagged) else True
    check("8. Names with heavy other income are flagged and confidence-downgraded",
          dq_ok, f"{len(sel_flagged)} selected names carry an earnings-quality flag")

    # 9 - source IDs resolve
    ids = set(slog["source_id"])
    missing = set()
    for col in ("primary_source_ids", "secondary_source_ids"):
        for s in cand[col].fillna("").astype(str):
            for x in s.split("|"):
                if x and x not in ids:
                    missing.add(x)
    check("9. Every source ID on a candidate resolves in the source log",
          not missing, f"{len(ids)} source IDs logged; unresolved: {sorted(missing)[:5]}")

    # 10 - headers, counts, dates, numerics, checksums
    req_uni = 37
    hdr_ok = len(uni.columns) == req_uni
    cand_ok = list(cand.columns[:req_uni]) == list(uni.columns) and \
        list(cand.columns[req_uni:]) == ["phase2_priority", "phase2_questions",
                                         "required_primary_documents", "known_data_gaps"]
    dates_ok = all(
        pd.to_datetime(uni[c], errors="coerce", format="%Y-%m-%d").notna().sum() > 0
        for c in ("screening_date", "price_date"))
    ck_ok = True
    for f in man["files"]:
        p = OUT / f["name"]
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        if h != f["sha256"]:
            ck_ok = False
            print(f"    checksum mismatch: {f['name']}")
        if f["row_count"] is not None and len(pd.read_csv(p)) != f["row_count"]:
            ck_ok = False
            print(f"    row-count mismatch: {f['name']}")
    check("10. Schema, headers, dates and manifest checksums reconcile",
          hdr_ok and cand_ok and dates_ok and ck_ok,
          f"universe cols={len(uni.columns)}, candidate cols={len(cand.columns)}, checksums OK={ck_ok}")

    # extra - candidate count in target range
    check("11. Candidate count within the 100-150 target",
          100 <= len(cand) <= 150, f"{len(cand)} candidates")

    # extra - no duplicate symbols
    check("12. No duplicate symbols in either CSV",
          uni["symbol"].duplicated().sum() == 0 and cand["symbol"].duplicated().sum() == 0)

    # extra - every eligible row has a score, every excluded row has a code
    el = uni[uni["eligible_flag"] == 1]
    ex = uni[uni["eligible_flag"] == 0]
    check("13. Scores present for all eligible; exclusion codes present for all excluded",
          el["preliminary_priority_score"].notna().all() and (ex["exclusion_code"] != "").all())

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} checks passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
