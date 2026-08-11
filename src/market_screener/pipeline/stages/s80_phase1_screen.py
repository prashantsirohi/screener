"""
Phase 1 screen: eligibility, three-axis classification, priority score.

Reads the store, writes run-versioned rows to phase1_universe and its satellite
tables. Emits no files - that is s85's job - so the screen can be re-run and
diffed without touching artifacts.

Everything here is orchestration. The arithmetic lives in domain/: metrics,
archetypes, scoring and eligibility are all verbatim ports of the frozen oracle,
each covered by a parity test.
"""

from __future__ import annotations

import logging
from datetime import date

from ...analytics import features
from ...db.copy_io import copy_rows, create_staging, drop_staging
from ...domain import archetypes, eligibility, fundamentals_view as fv
from ...domain import metrics as metrics_mod
from ...domain.scoring import score_priority
from ...ingest import classify_events
from ..context import RunContext, StageResult

log = logging.getLogger(__name__)

STAGE = "s80_phase1_screen"

UNIVERSE_COLS = (
    "run_id", "security_id", "screening_date", "company", "symbol", "exchange",
    "listing_board", "sector", "industry", "current_price_inr", "price_date",
    "market_cap_inr_cr", "market_cap_date", "market_cap_method",
    "liquidity_metric_name", "liquidity_value_inr_cr", "liquidity_period",
    "eligible_flag", "exclusion_code", "exclusion_reason", "primary_archetype",
    "classification_rationale", "secondary_tags", "technical_stage",
    "technical_data_date", "revenue_cagr_5y_pct", "normalized_eps_cagr_5y_pct",
    "latest_roce_or_roe_pct", "median_roce_or_roe_5y_pct", "net_debt_to_equity",
    "cfo_pat_period", "cfo_pat_ratio", "preliminary_valuation_method",
    "preliminary_valuation_value", "preliminary_priority_score", "inclusion_reason",
    "key_disqualifying_risk", "data_quality_confidence", "primary_source_ids",
    "secondary_source_ids",
)

UNIVERSE_STAGING = {
    "run_id": "text", "security_id": "bigint", "screening_date": "date",
    "company": "text", "symbol": "text", "exchange": "text", "listing_board": "text",
    "sector": "text", "industry": "text", "current_price_inr": "numeric",
    "price_date": "date", "market_cap_inr_cr": "numeric", "market_cap_date": "date",
    "market_cap_method": "text", "liquidity_metric_name": "text",
    "liquidity_value_inr_cr": "numeric", "liquidity_period": "text",
    "eligible_flag": "smallint", "exclusion_code": "text", "exclusion_reason": "text",
    "primary_archetype": "text", "classification_rationale": "text",
    "secondary_tags": "text", "technical_stage": "text", "technical_data_date": "date",
    "revenue_cagr_5y_pct": "numeric", "normalized_eps_cagr_5y_pct": "numeric",
    "latest_roce_or_roe_pct": "numeric", "median_roce_or_roe_5y_pct": "numeric",
    "net_debt_to_equity": "numeric", "cfo_pat_period": "text",
    "cfo_pat_ratio": "numeric", "preliminary_valuation_method": "text",
    "preliminary_valuation_value": "numeric", "preliminary_priority_score": "numeric",
    "inclusion_reason": "text", "key_disqualifying_risk": "text",
    "data_quality_confidence": "text", "primary_source_ids": "text",
    "secondary_source_ids": "text",
}


def _market_cap(m: dict, close: float | None, face_value: float | None,
                bs_period: str | None) -> tuple[float | None, str]:
    """
    Aggregator value first; otherwise derive it from primary data.

    The derived route needs equity capital, which comes from the same page - so
    it cannot rescue a blank shell. It is a cross-check and a fallback for a
    stale aggregator figure, not the blank-page fix; the retry queue is that.
    """
    mc = m.get("market_cap_inr_cr")
    if mc is not None:
        return mc, "Aggregator reported"
    eqc = m.get("equity_capital_latest_inr_cr")
    recent = (bs_period or "").endswith(("2025", "2026"))
    if eqc and face_value and close and face_value > 0 and recent:
        return close * (eqc / face_value), (
            f"Computed: NSE close x (equity capital / face value), BS {bs_period}")
    return None, "Unavailable"


def reuse(ctx: RunContext, prior_run_id: str) -> StageResult:
    """
    Carry a previous run's screen forward instead of recomputing it.

    Skipping the compute is only safe if the new run still HAS the rows: every
    run is self-contained so that two runs can be diffed, and s85 reads
    phase1_universe for its own run_id. Skipping without copying left the new run
    empty and the next stage failed on a missing column.
    """
    db = ctx.db
    with db.transaction() as conn, conn.cursor() as cur:
        for table, cols in (
            ("phase1_universe", ", ".join(UNIVERSE_COLS[2:])),
            ("phase1_score_component", "component, weight, value"),
            ("phase1_archetype_fit", "archetype, fit_score, is_primary"),
        ):
            cur.execute(f"""
                INSERT INTO market.{table} (run_id, security_id, {cols})
                SELECT %s, security_id, {cols}
                FROM   market.{table} WHERE run_id = %s
                ON CONFLICT DO NOTHING
            """, (ctx.run_id, prior_run_id))

    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.phase1_universe WHERE run_id = %s",
        (ctx.run_id,))
    el = db.fetch_value(
        "SELECT count(*) AS c FROM market.phase1_universe "
        "WHERE run_id = %s AND eligible_flag = 1", (ctx.run_id,))
    ctx.state["evaluated"], ctx.state["eligible"] = n, el
    log.info("carried %d rows forward from %s", n, prior_run_id)
    return StageResult(stage=STAGE, status="skipped",
                       skip_reason="unchanged_stage_inputs",
                       rows_out=n, detail={"evaluated": n, "eligible": el,
                                           "carried_from": prior_run_id})


def run(ctx: RunContext) -> StageResult:
    db, st, as_of = ctx.db, ctx.settings, ctx.as_of

    universe = db.fetch_all("""
        SELECT s.security_id, s.symbol, s.company_name, s.nse_industry, s.face_value,
               p.close AS close_inr, p.trade_date AS price_date
        FROM   market.security s
        LEFT JOIN LATERAL (
            SELECT close, trade_date FROM market.price_daily
            WHERE  security_id = s.security_id AND trade_date <= %s
            ORDER  BY trade_date DESC LIMIT 1
        ) p ON true
        WHERE  s.is_active AND s.series = 'EQ' AND s.security_type = 'equity'
        ORDER  BY s.symbol
    """, (as_of,))
    log.info("screening %d active series-EQ securities", len(universe))

    tech_by_id = {r["security_id"]: r
                  for r in features.compute_universe(st, as_of)}
    labels = fv._label_lookup(db)

    events_by_symbol: dict[str, list[dict]] = {}
    for r in classify_events.event_flags(db, "v1"):
        events_by_symbol.setdefault(r["symbol"], []).append({
            "event_class": r["event_class"],
            "latest_date": str(r["latest_date"]),
            "headline": r["headline"] or ""})

    price_date = db.fetch_value(
        "SELECT max(trade_date) AS d FROM market.price_daily WHERE trade_date <= %s",
        (as_of,))

    rows, components, fits, derived = [], [], [], []
    eligible = 0

    for u in universe:
        sid, sym = u["security_id"], u["symbol"]
        industry = u["nse_industry"]

        m = metrics_mod.compute(fv.payload_for_metrics(db, sid, labels=labels), industry)
        tech = tech_by_id.get(sid) or {
            "technical_stage": "Indeterminate-insufficient adjusted history",
            "stage_rationale": "no adjusted weekly price history retrieved"}

        mc, mc_method = _market_cap(
            m, float(u["close_inr"]) if u["close_inr"] is not None else None,
            float(u["face_value"]) if u["face_value"] is not None else None,
            m.get("balance_sheet_latest_period"))
        m["market_cap_inr_cr"] = mc          # archetype tests read it from here

        liq = tech.get("liquidity_inr_cr")
        weeks = tech.get("weeks_history")
        verdict = eligibility.assess(m, mc, weeks, liq)

        evs = events_by_symbol.get(sym, [])
        cls = {"primary_archetype": None, "archetype_fit": 0.0,
               "classification_rationale": None, "archetype_scores": {}}
        tags: list[str] = []
        score, breakdown = None, {}

        if verdict.eligible:
            cls = archetypes.classify(m, evs)
            if not cls["primary_archetype"]:
                verdict = eligibility.Eligibility(
                    False, "EX_NO_ARCHETYPE",
                    eligibility.EXCLUSION_CODES["EX_NO_ARCHETYPE"])
            else:
                tags = archetypes.secondary_tags(m, tech, cls["primary_archetype"], evs)
                score, breakdown = score_priority(m, tech, cls["archetype_fit"], liq)
                eligible += 1

        fin = bool(m.get("is_financial"))
        rows.append((
            ctx.run_id, sid, as_of,
            m.get("company") or u["company_name"], sym, "NSE", "Main Board",
            industry or "Not disclosed", industry or "Not disclosed",
            m.get("current_price_inr") or u["close_inr"], u["price_date"] or price_date,
            mc, price_date, mc_method,
            "3-month median daily traded value", liq,
            tech.get("liquidity_period") or "13-week median of weekly traded value / 5",
            1 if verdict.eligible else 0,
            verdict.code or "", verdict.reason or "",
            cls.get("primary_archetype") or "",
            cls.get("classification_rationale") or "",
            "|".join(tags),
            tech.get("technical_stage"), tech.get("technical_data_date"),
            m.get("revenue_cagr_5y_pct"), m.get("eps_cagr_5y_pct"),
            m.get("roe_latest_pct") if fin else m.get("roce_latest_pct"),
            m.get("screener_roe_5y") if fin else m.get("roce_median_5y_pct"),
            m.get("net_debt_to_equity"), m.get("cfo_pat_period") or "",
            m.get("cfo_pat_5y") if not fin else None,
            ("P/B vs ROE (preliminary)" if fin
             else "Trailing P/E vs 3-5y EPS CAGR (preliminary)"),
            m.get("stock_pe"), score,
            cls.get("classification_rationale") or "",
            eligibility.key_risk(m, tech.get("technical_stage")),
            eligibility.data_quality(m),
            "", "",                      # source IDs filled in below
        ))

        for comp, val in (breakdown or {}).items():
            weight = float(comp.rsplit("_", 1)[-1])
            components.append((ctx.run_id, sid, comp, weight, val))
        for arch, fit in (cls.get("archetype_scores") or {}).items():
            fits.append((ctx.run_id, sid, arch, fit,
                         arch == cls.get("primary_archetype")))

        # Persist the derived metrics QC audits against. Without these, checks
        # like "no cyclical selected at peak margin" have nothing to test and
        # pass vacuously.
        for name in ("opm_vs_peak_pp", "opm_latest_pct", "opm_peak_5y_pct",
                     "cwip_to_gross_block_pct", "q_opm_yoy_delta_pp",
                     "promoter_change_pct", "equity_capital_change_5y_pct"):
            v = m.get(name)
            if v is not None:
                derived.append((sid, as_of, name, float(v), None,
                                "domain.metrics", ctx.run_id))
        if m.get("earnings_quality_flag"):
            derived.append((sid, as_of, "earnings_quality_flag", None,
                            m["earnings_quality_flag"], "domain.metrics", ctx.run_id))

    _write(ctx, rows, components, fits, derived)
    ctx.state["eligible"] = eligible
    ctx.state["evaluated"] = len(rows)

    return StageResult(stage=STAGE, rows_in=len(universe), rows_out=len(rows),
                       detail={"evaluated": len(rows), "eligible": eligible,
                               "with_technicals": len(tech_by_id)})


def _write(ctx: RunContext, rows, components, fits, derived) -> None:
    db = ctx.db
    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "p1_in", UNIVERSE_STAGING)
        copy_rows(cur, "p1_in", UNIVERSE_COLS, rows)
        cur.execute(f"""
            INSERT INTO market.phase1_universe ({", ".join(UNIVERSE_COLS)})
            SELECT {", ".join(UNIVERSE_COLS)} FROM staging.p1_in
            ON CONFLICT (run_id, security_id) DO NOTHING
        """)

        create_staging(cur, "p1_comp", {
            "run_id": "text", "security_id": "bigint", "component": "text",
            "weight": "numeric", "value": "numeric"})
        copy_rows(cur, "p1_comp",
                  ("run_id", "security_id", "component", "weight", "value"), components)
        cur.execute("""
            INSERT INTO market.phase1_score_component
                (run_id, security_id, component, weight, value)
            SELECT run_id, security_id, component, weight, value FROM staging.p1_comp
            ON CONFLICT DO NOTHING
        """)

        create_staging(cur, "p1_fit", {
            "run_id": "text", "security_id": "bigint", "archetype": "text",
            "fit_score": "numeric", "is_primary": "boolean"})
        copy_rows(cur, "p1_fit",
                  ("run_id", "security_id", "archetype", "fit_score", "is_primary"), fits)
        cur.execute("""
            INSERT INTO market.phase1_archetype_fit
                (run_id, security_id, archetype, fit_score, is_primary)
            SELECT run_id, security_id, archetype, fit_score, is_primary
            FROM   staging.p1_fit ON CONFLICT DO NOTHING
        """)

        create_staging(cur, "p1_dm", {
            "security_id": "bigint", "as_of_date": "date", "metric": "text",
            "value_num": "numeric", "value_text": "text", "computed_by": "text",
            "run_id": "text"})
        copy_rows(cur, "p1_dm",
                  ("security_id", "as_of_date", "metric", "value_num", "value_text",
                   "computed_by", "run_id"), derived)
        cur.execute("""
            INSERT INTO market.company_metric
                (security_id, as_of_date, metric, value_num, value_text,
                 computed_by, run_id)
            SELECT DISTINCT ON (security_id, as_of_date, metric)
                   security_id, as_of_date, metric, value_num, value_text,
                   computed_by, run_id
            FROM   staging.p1_dm
            ON CONFLICT (security_id, as_of_date, metric) DO UPDATE SET
                value_num = EXCLUDED.value_num, value_text = EXCLUDED.value_text,
                run_id = EXCLUDED.run_id, computed_at = now()
        """)
        drop_staging(cur, "p1_in", "p1_comp", "p1_fit", "p1_dm")
