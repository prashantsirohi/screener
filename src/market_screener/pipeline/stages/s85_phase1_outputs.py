"""
Phase 1 outputs: candidate selection and the CSV hand-off.

The 37-column contract is frozen and reproduced exactly. `market_cap_method`
exists in the database but is deliberately NOT in the CSV: the contract is what
Phase 2 consumes, and widening it silently would break a downstream reader.

The source log is generated from real ingest provenance rather than asserted.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ...db.copy_io import copy_rows, create_staging, drop_staging
from ...domain import provenance
from ..context import RunContext, StageArtifact, StageResult

log = logging.getLogger(__name__)

STAGE = "s85_phase1_outputs"

# The frozen contract. Order and spelling are load-bearing.
SCREEN_COLS = [
    "screening_date", "company", "symbol", "exchange", "listing_board", "sector",
    "industry", "current_price_inr", "price_date", "market_cap_inr_cr",
    "market_cap_date", "liquidity_metric_name", "liquidity_value_inr_cr",
    "liquidity_period", "eligible_flag", "exclusion_code", "exclusion_reason",
    "primary_archetype", "classification_rationale", "secondary_tags",
    "technical_stage", "technical_data_date", "revenue_cagr_5y_pct",
    "normalized_eps_cagr_5y_pct", "latest_roce_or_roe_pct",
    "median_roce_or_roe_5y_pct", "net_debt_to_equity", "cfo_pat_period",
    "cfo_pat_ratio", "preliminary_valuation_method", "preliminary_valuation_value",
    "preliminary_priority_score", "inclusion_reason", "key_disqualifying_risk",
    "data_quality_confidence", "primary_source_ids", "secondary_source_ids",
]
CANDIDATE_EXTRA = ["phase2_priority", "phase2_questions",
                   "required_primary_documents", "known_data_gaps"]

# Fallbacks only. The live values come from ctx.settings.screen - these constants
# used to be the real ones, which quietly made candidate_target_high and
# min_select_score dead config: both were folded into config_hash, so changing
# them produced a different hash and an identical candidate set.
TARGET_LO, TARGET_HI = 100, 150
MIN_SELECT_SCORE = 60.0

ARCHETYPE_QUESTIONS = {
    "Quality compounder": [
        "Is the 5y median ROCE sustained on incremental capital, or inflated by a legacy asset base?",
        "What is the reinvestment runway, and has capital allocation stayed inside the core?",
        "Does cumulative CFO/PAT hold above 0.8 once working-capital swings are normalised?"],
    "High-growth company": [
        "Is forward growth contracted/visible or extrapolated from a favourable base?",
        "Do unit economics hold as mix shifts, and is growth funded without dilution?",
        "How much of EPS growth is operating vs. below-the-line or tax-rate driven?"],
    "Capex operating-leverage candidate": [
        "What exactly has been commissioned, on what date, and at what capex vs. original budget?",
        "What demand evidence exists (orders, nominations, approvals, contracts) for the new capacity?",
        "What are incremental EBITDA margin and incremental ROCE at 50/70/85% utilisation?",
        "How do 6/12/18-month commissioning delays change FY+2 EPS and the debt schedule?"],
    "Cyclical recovery": [
        "Where is the cycle versus mid-cycle margin, and what capacity is still being added industry-wide?",
        "Is the improvement demand-led or a one-off inventory/input-cost effect?",
        "Does the balance sheet survive another 4-6 quarters at trough spreads?"],
    "Turnaround": [
        "What concretely changed - management, business model, cost base, or only the narrative?",
        "What is the funding runway to EBITDA breakeven and what must be refinanced?",
        "Is working capital genuinely releasing cash, or are receivables/inventory masking it?"],
    "Asset-value/SOTP opportunity": [
        "What are conservative realisable values for each asset, net of tax leakage and holdco discount?",
        "Is there a disclosed unlocking mechanism with a timeline, or only a static discount?",
        "Does the core operating business earn above its cost of capital?"],
    "Financial compounder": [
        "Is ROE sustainable on current capital, and what dilution is required to fund growth?",
        "What are GNPA/NNPA, provision coverage, and credit-cost trend through the cycle?",
        "What is the funding franchise - cost of funds trajectory and liability mix?"],
    "Mature value/yield company": [
        "Is the dividend covered by normalised FCF after maintenance capex, not by peak earnings?",
        "What structural-decline risk exists in the core end-market?",
        "Is management willing to keep returning capital, on what stated policy?"],
    "Event-driven or special situation": [
        "What is the disclosed scheme/transaction structure, record date, and regulatory path?",
        "What is the value in completed, delayed and cancelled scenarios, and each probability?",
        "What is the downside if the event does not complete at all?"],
    "Speculative/emerging business": [
        "What is the cash runway in quarters, and what dilution closes the gap to breakeven?",
        "Are unit economics positive at the contribution level today?",
        "What explicit failure probability and residual value should be modelled?"],
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


def _gaps(r: pd.Series, is_financial: bool) -> str:
    g = []
    if pd.isna(r.get("revenue_cagr_5y_pct")):
        g.append("5y revenue CAGR not computable (short/loss-making history)")
    if pd.isna(r.get("normalized_eps_cagr_5y_pct")):
        g.append("5y EPS CAGR not computable (negative or zero base year)")
    if pd.isna(r.get("cfo_pat_ratio")) and not is_financial:
        g.append("cumulative CFO/PAT unavailable")
    if pd.isna(r.get("median_roce_or_roe_5y_pct")):
        g.append("5y median return metric unavailable")
    g.append("EPS not normalised for exceptional items at Phase 1 - reported basis only")
    g.append("Promoter pledging not captured by the aggregator - confirm from filings")
    return "; ".join(g)


def run(ctx: RunContext) -> StageResult:
    db, as_of = ctx.db, ctx.as_of

    # ---- provenance, then backfill the source IDs onto the run's rows -------
    sec_ids = [r["security_id"] for r in db.fetch_all(
        "SELECT security_id FROM market.phase1_universe WHERE run_id = %s",
        (ctx.run_id,))]
    records = provenance.global_sources(db, as_of, ctx.pit_cutoff) + \
        provenance.company_sources(db, sec_ids, as_of, ctx.pit_cutoff)

    id_rows = []
    for sid in sec_ids:
        prim, sec = provenance.source_ids_for(records, sid)
        id_rows.append((ctx.run_id, sid, prim, sec))
    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "p1_src", {
            "run_id": "text", "security_id": "bigint",
            "primary_source_ids": "text", "secondary_source_ids": "text"})
        copy_rows(cur, "p1_src",
                  ("run_id", "security_id", "primary_source_ids",
                   "secondary_source_ids"), id_rows)
        cur.execute("""
            UPDATE market.phase1_universe u
            SET    primary_source_ids = s.primary_source_ids,
                   secondary_source_ids = s.secondary_source_ids
            FROM   staging.p1_src s
            WHERE  u.run_id = s.run_id AND u.security_id = s.security_id
        """)
        create_staging(cur, "p1_srclog", {
            "run_id": "text", "source_id": "text", "security_id": "bigint",
            "company": "text", "symbol": "text", "document_type": "text",
            "title": "text", "issuer": "text", "published_date": "date",
            "period_covered": "text", "url": "text", "primary_or_secondary": "text",
            "accessed_date": "date", "claim_or_metric_supported": "text",
            "notes": "text"})
        copy_rows(cur, "p1_srclog",
                  ("run_id", "source_id", "security_id", "company", "symbol",
                   "document_type", "title", "issuer", "published_date",
                   "period_covered", "url", "primary_or_secondary", "accessed_date",
                   "claim_or_metric_supported", "notes"),
                  [(ctx.run_id, r.source_id, r.security_id, r.company, r.symbol,
                    r.document_type, r.title, r.issuer, r.published_date,
                    r.period_covered, r.url, r.primary_or_secondary, r.accessed_date,
                    r.claim_or_metric_supported, r.notes) for r in records])
        cur.execute("""
            INSERT INTO market.screen_source_log
                (run_id, source_id, security_id, company, symbol, document_type,
                 title, issuer, published_date, period_covered, url,
                 primary_or_secondary, accessed_date, claim_or_metric_supported, notes)
            SELECT DISTINCT ON (run_id, source_id)
                   run_id, source_id, security_id, company, symbol, document_type,
                   title, issuer, published_date, period_covered, url,
                   primary_or_secondary, accessed_date, claim_or_metric_supported, notes
            FROM   staging.p1_srclog
            ON CONFLICT (run_id, source_id) DO NOTHING
        """)
        drop_staging(cur, "p1_src", "p1_srclog")

    # ---- selection ----------------------------------------------------------
    uni = pd.DataFrame(db.fetch_all("""
        SELECT * FROM market.phase1_universe WHERE run_id = %s ORDER BY symbol
    """, (ctx.run_id,)))
    el = uni[uni["eligible_flag"] == 1].copy()
    el["preliminary_priority_score"] = pd.to_numeric(
        el["preliminary_priority_score"], errors="coerce")
    el = el.sort_values("preliminary_priority_score", ascending=False)

    floor = ctx.settings.screen.min_select_score
    target_hi = ctx.settings.screen.candidate_target_high
    pool = el[el["preliminary_priority_score"] >= floor]

    # Which constraint bound is the informative part. A run capped at the target
    # says the market offered more than we can research; a run stopped by the
    # floor says it did not, and the count is then a signal rather than a
    # constant. Both are recorded so the summary can say which happened.
    if len(pool) > target_hi:
        cand = pool.head(target_hi).copy()
        bound_by = "target"
        note = (f"top {target_hi} by preliminary priority score "
                f"(cut at {cand['preliminary_priority_score'].min():.1f}; "
                f"{len(pool)} cleared the {floor:.0f} floor)")
    else:
        cand = pool.copy()
        bound_by = "floor"
        note = (f"all {len(cand)} eligible names scoring >= {floor:.0f} - the "
                f"hard floor bound before the {target_hi} target")
    ctx.state["selection_bound_by"] = bound_by
    ctx.state["score_floor"] = floor

    if len(cand):
        q1, q2 = cand["preliminary_priority_score"].quantile([1 / 3, 2 / 3])
        cand["phase2_priority"] = cand["preliminary_priority_score"].apply(
            lambda s: "High" if s >= q2 else ("Medium" if s >= q1 else "Low"))
        cand["phase2_questions"] = cand["primary_archetype"].map(
            lambda a: " | ".join(ARCHETYPE_QUESTIONS.get(a, [])))
        cand["required_primary_documents"] = cand["primary_archetype"].map(
            lambda a: ARCHETYPE_DOCS.get(
                a, "Annual report; quarterly results; investor presentation"))
        fin_ids = set(db.fetch_value(
            "SELECT coalesce(array_agg(security_id), '{}') AS ids FROM market.security "
            "WHERE nse_industry ILIKE '%%financial%%'") or [])
        cand["known_data_gaps"] = [
            _gaps(r, r["security_id"] in fin_ids) for _, r in cand.iterrows()]
        cand = cand.sort_values(["preliminary_priority_score", "primary_archetype"],
                                ascending=[False, True])
        cand["rank"] = range(1, len(cand) + 1)

        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "p1_cand", {
                "run_id": "text", "security_id": "bigint", "rank": "integer",
                "phase2_priority": "text", "phase2_questions": "text",
                "required_primary_documents": "text", "known_data_gaps": "text"})
            copy_rows(cur, "p1_cand",
                      ("run_id", "security_id", "rank", "phase2_priority",
                       "phase2_questions", "required_primary_documents",
                       "known_data_gaps"),
                      [(ctx.run_id, int(r.security_id), int(r["rank"]),
                        r.phase2_priority, r.phase2_questions,
                        r.required_primary_documents, r.known_data_gaps)
                       for _, r in cand.iterrows()])
            cur.execute("""
                INSERT INTO market.phase1_candidate
                    (run_id, security_id, rank, phase2_priority, phase2_questions,
                     required_primary_documents, known_data_gaps)
                SELECT run_id, security_id, rank, phase2_priority, phase2_questions,
                       required_primary_documents, known_data_gaps
                FROM   staging.p1_cand ON CONFLICT DO NOTHING
            """)
            drop_staging(cur, "p1_cand")

    # ---- files --------------------------------------------------------------
    out = ctx.output_dir()
    artifacts: list[StageArtifact] = []

    uni_out = uni.sort_values("symbol")[SCREEN_COLS]
    p = out / "P1_screened_universe.csv"
    uni_out.to_csv(p, index=False, encoding="utf-8")
    artifacts.append(StageArtifact("P1_screened_universe.csv", "csv", p, len(uni_out)))

    cand_out = (cand[SCREEN_COLS + CANDIDATE_EXTRA] if len(cand)
                else pd.DataFrame(columns=SCREEN_COLS + CANDIDATE_EXTRA))
    p = out / "P1_candidates.csv"
    cand_out.to_csv(p, index=False, encoding="utf-8")
    artifacts.append(StageArtifact("P1_candidates.csv", "csv", p, len(cand_out)))

    slog = pd.DataFrame(db.fetch_all("""
        SELECT source_id, company, symbol, document_type, title, issuer,
               published_date, period_covered, url, primary_or_secondary,
               accessed_date, claim_or_metric_supported, notes
        FROM   market.screen_source_log WHERE run_id = %s ORDER BY source_id
    """, (ctx.run_id,)))
    p = out / "P1_source_log.csv"
    slog.to_csv(p, index=False, encoding="utf-8")
    artifacts.append(StageArtifact("P1_source_log.csv", "csv", p, len(slog)))

    ctx.state["selected"] = len(cand)
    ctx.state["selection_note"] = note
    log.info("selected %d candidates (%s); %d source records",
             len(cand), note, len(slog))

    return StageResult(stage=STAGE, rows_in=len(uni), rows_out=len(cand),
                       artifacts=artifacts,
                       detail={"selected": len(cand), "bound_by": bound_by,
                               "selection_note": note,
                               "source_records": len(slog)})
