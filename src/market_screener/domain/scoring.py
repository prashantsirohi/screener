"""
Phase 1 preliminary priority score.

Extracted verbatim from the frozen `04_phase1_screen.py` oracle so the six
weighted components and every threshold survive the port unchanged. Any change
here moves the candidate ranking, so it is deliberately isolated in its own
module with a parity test over it.

Weights, as specified in the brief:
    financial and balance-sheet quality   20
    evidence for the archetype thesis     25
    forward catalyst visibility           20
    preliminary valuation plausibility    15
    governance and disclosure quality     10
    technical and liquidity confirmation  10
"""

from __future__ import annotations

def score_priority(m: dict, tech: dict, fit: float, liq: float | None) -> tuple[float, dict]:
    """Phase 1 preliminary priority score, 0-100, on the prescribed weights."""
    fin = bool(m.get("is_financial"))
    b = {}

    # --- Financial & balance-sheet quality: 20 ---
    q = 0.0
    if fin:
        roe = m.get("roe_latest_pct")
        q += min(8, max(0, ((roe or 0) - 8) / 12 * 8))
        dil = m.get("equity_capital_change_5y_pct")
        q += 6 if (dil is not None and dil < 20) else (3 if dil is not None and dil < 45 else 0)
        q += min(6, max(0, ((m.get("screener_roe_5y") or 0) - 8) / 12 * 6))
    else:
        cp = m.get("cfo_pat_5y") or m.get("cfo_pat_3y")
        q += min(7, max(0, ((cp or 0) - 0.3) / 0.7 * 7))
        de = m.get("net_debt_to_equity")
        q += 6 if (de is not None and de < 0.4) else (4 if (de is not None and de < 1.0)
                                                     else (2 if (de is not None and de < 1.8) else 0))
        ic = m.get("interest_cover_x")
        q += 4 if (ic is not None and ic > 5) else (2 if (ic is not None and ic > 2.5) else 0)
        fp = m.get("fcf_positive_years_5y")
        q += min(3, (fp or 0) / 5 * 3)
    b["financial_quality_20"] = round(min(20, q), 1)

    # --- Archetype thesis evidence: 25 ---
    b["archetype_evidence_25"] = round(min(25, fit / 100 * 25), 1)

    # --- Forward catalyst visibility: 20 ---
    c = 0.0
    qr, qp, qd = m.get("q_revenue_yoy_pct"), m.get("q_profit_yoy_pct"), m.get("q_opm_yoy_delta_pp")
    c += min(6, max(0, (qr or 0) / 25 * 6))
    c += min(6, max(0, (qp or 0) / 40 * 6))
    c += min(5, max(0, (qd or 0) / 4 * 5))
    cwr = m.get("cwip_to_gross_block_pct")
    c += 3 if (cwr is not None and cwr > 12) else 0
    b["catalyst_visibility_20"] = round(min(20, c), 1)

    # --- Preliminary valuation plausibility: 15 ---
    v = 0.0
    pe = m.get("stock_pe")
    growth = max([x for x in (m.get("eps_cagr_3y_pct"), m.get("eps_cagr_5y_pct")) if x is not None],
                 default=None)
    if pe is not None and pe > 0:
        if growth and growth > 0:
            peg = pe / growth
            v += 9 if peg < 1.0 else (7 if peg < 1.6 else (4 if peg < 2.5 else 1))
        else:
            v += 5 if pe < 20 else (3 if pe < 32 else 0)
        if pe > 80:
            v -= 3
    bv, px = m.get("book_value_inr"), m.get("current_price_inr")
    roe = m.get("roe_latest_pct")
    if bv and px and bv > 0:
        pb = px / bv
        if roe:
            v += 4 if (pb < roe / 8) else (2 if pb < roe / 5 else 0)
        else:
            v += 2 if pb < 3 else 0
    dy = m.get("dividend_yield_pct")
    v += min(2, (dy or 0) / 3 * 2)
    b["valuation_plausibility_15"] = round(max(0, min(15, v)), 1)

    # --- Governance & disclosure: 10 ---
    g = 0.0
    prom = m.get("promoter_holding_pct")
    if prom is not None:
        g += 3 if prom >= 40 else (2 if prom >= 25 else 1)
    pch = m.get("promoter_change_pct")
    if pch is not None:
        g += 3 if pch >= -0.5 else (1 if pch > -3 else 0)
    dil = m.get("equity_capital_change_5y_pct")
    g += 2 if (dil is not None and dil < 15) else (1 if (dil is not None and dil < 40) else 0)
    g += 2 if not m.get("earnings_quality_flag") else 0
    b["governance_10"] = round(min(10, g), 1)

    # --- Technical & liquidity confirmation: 10 ---
    t = 0.0
    stage = tech.get("technical_stage")
    t += {"Stage 1-to-Stage 2 transition": 5.0, "Early Stage 2": 4.5,
          "Mature Stage 1 base": 4.0, "Early Stage 1": 2.5,
          "Mature/extended Stage 2": 2.0, "Stage 3 distribution": 0.5,
          "Stage 4 decline": 0.0}.get(stage, 1.0)
    rs = tech.get("rs_bm_13w_pct")
    t += 3 if (rs is not None and rs > 5) else (2 if (rs is not None and rs > 0) else 0)
    if liq is not None:
        t += 2 if liq > 10 else (1.5 if liq > 3 else (1 if liq > 1 else 0))
    b["technical_liquidity_10"] = round(min(10, t), 1)

    total = sum(b.values())
    return round(total, 1), b

