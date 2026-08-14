"""
Axis A/B classification: archetype fit scoring and secondary tags.

Each archetype has its own discovery test (per the brief - no single universal
screen). Every archetype returns a 0-100 fit score plus the evidence strings that
produced it. The company is assigned the ONE archetype with the highest fit,
which is the mechanism expected to drive most of the shareholder return.

Where the data cannot support an archetype's test at all, the fit is 0 - never a
default. "Event-driven" is deliberately hard to score from financial statements
alone and is flagged as under-detected in the Phase 1 summary.
"""

from __future__ import annotations


def _g(m: dict, k: str, default=None):
    v = m.get(k)
    return default if v is None else v


# Industries whose earnings are driven by a commodity or freight cycle. A high
# trailing EPS CAGR in these sectors usually measures the cycle, not compounding,
# so the quality/high-growth tests are damped and peak-cycle risk is surfaced.
CYCLICAL_INDUSTRIES = {
    "Metals & Mining", "Oil Gas & Consumable Fuels", "Construction Materials",
    "Chemicals", "Power", "Textiles", "Forest Materials",
}
CYCLICAL_NAME_HINTS = (
    "petroleum", "refiner", "steel", "aluminium", "aluminum", "copper", "zinc",
    "cement", "sugar", "shipping", "paper", "fertiliser", "fertilizer",
    "petrochem", "commodity", "iron", "ferro", "coal", "mining", "smelt",
)


def is_cyclical(m: dict) -> bool:
    ind = (m.get("industry") or "")
    if ind in CYCLICAL_INDUSTRIES:
        return True
    nm = (m.get("company") or "").lower()
    return any(h in nm for h in CYCLICAL_NAME_HINTS)


def at_or_near_peak_margin(m: dict, tol: float = 1.5) -> bool:
    opm, peak = m.get("opm_latest_pct"), m.get("opm_peak_5y_pct")
    if opm is None or peak is None:
        return False
    return opm >= peak - tol


def _pos(v, lo, hi, pts):
    """Linear points between lo and hi, clipped to [0, pts]."""
    if v is None:
        return 0.0
    if hi == lo:
        return pts if v >= hi else 0.0
    x = (v - lo) / (hi - lo)
    return max(0.0, min(1.0, x)) * pts


def fit_quality_compounder(m):
    if m.get("is_financial"):
        return 0, []
    ev = []
    s = 0.0
    s += _pos(m.get("revenue_cagr_5y_pct"), 8, 20, 22)
    s += _pos(m.get("reported_eps_cagr_5y_pct"), 10, 25, 22)
    s += _pos(m.get("roce_median_5y_pct"), 14, 28, 26)
    cp = m.get("cfo_pat_5y") or m.get("cfo_pat_3y")
    s += _pos(cp, 0.5, 1.0, 15)
    de = m.get("gross_debt_to_equity")
    s += 10 if (de is not None and de < 0.5) else (5 if (de is not None and de < 1.0) else 0)
    opm_stab = m.get("opm_median_5y_pct")
    s += _pos(opm_stab, 10, 25, 5)
    if (m.get("roce_median_5y_pct") or 0) > 18:
        ev.append(f"5y median ROCE {m['roce_median_5y_pct']:.0f}%")
    if (m.get("revenue_cagr_5y_pct") or 0) > 12:
        ev.append(f"5y revenue CAGR {m['revenue_cagr_5y_pct']:.0f}%")
    if (m.get("reported_eps_cagr_5y_pct") or 0) > 15:
        ev.append(f"5y EPS CAGR {m['reported_eps_cagr_5y_pct']:.0f}%")
    if cp and cp >= 0.8:
        ev.append(f"cumulative CFO/PAT {cp:.2f}")
    # gate: a compounder must actually earn above a rough cost of capital
    if (m.get("roce_median_5y_pct") or 0) < 14 or (cp is not None and cp < 0.5):
        s *= 0.45
    # A commodity cyclical printing peak margins is not a compounder: the trailing
    # CAGR is measuring the cycle. Damp hard so the cyclical test wins instead.
    if is_cyclical(m) and at_or_near_peak_margin(m):
        s *= 0.35
        ev.append("cyclical sector at peak margin - trailing CAGR reflects the cycle")
    elif is_cyclical(m):
        s *= 0.75
    return s, ev


def fit_high_growth(m):
    if m.get("is_financial"):
        return 0, []
    ev = []
    s = 0.0
    r5, r3 = m.get("revenue_cagr_5y_pct"), m.get("revenue_cagr_3y_pct")
    e5, e3 = m.get("reported_eps_cagr_5y_pct"), m.get("reported_eps_cagr_3y_pct")
    qr = m.get("q_revenue_yoy_pct")
    s += _pos(max([x for x in (r5, r3) if x is not None], default=None), 15, 35, 28)
    s += _pos(max([x for x in (e5, e3) if x is not None], default=None), 18, 45, 26)
    s += _pos(qr, 12, 40, 20)
    cp = m.get("cfo_pat_5y") or m.get("cfo_pat_3y")
    s += _pos(cp, 0.3, 0.9, 12)
    de = m.get("gross_debt_to_equity")
    s += 8 if (de is not None and de < 0.8) else 0
    dil = m.get("equity_capital_change_5y_pct")
    s += 6 if (dil is not None and dil < 15) else 0
    if r3 and r3 > 18:
        ev.append(f"3y revenue CAGR {r3:.0f}%")
    if e3 and e3 > 20:
        ev.append(f"3y EPS CAGR {e3:.0f}%")
    if qr and qr > 15:
        ev.append(f"latest quarter revenue +{qr:.0f}% YoY")
    if dil is not None and dil > 40:
        s *= 0.6
    if is_cyclical(m) and at_or_near_peak_margin(m):
        s *= 0.4
        ev.append("cyclical sector at peak margin - growth is cycle-driven")
    return s, ev


def fit_capex_leverage(m):
    """
    Capex operating leverage requires evidence beyond an announcement. The
    balance-sheet tells available at Phase 1 are: a large CWIP block relative to
    gross block (build in progress), or CWIP that has just been capitalised into
    fixed assets (commissioning), combined with a fixed-asset base that has grown
    materially faster than revenue - i.e. capacity is in place but not yet earning.
    """
    if m.get("is_financial"):
        return 0, []
    ev = []
    s = 0.0
    cwip_ratio = m.get("cwip_to_gross_block_pct")
    fa, cwip, cwip_prev = m.get("fixed_assets_inr_cr"), m.get("cwip_inr_cr"), m.get("cwip_prev_inr_cr")
    if cwip_ratio is not None:
        s += _pos(cwip_ratio, 8, 45, 30)
        if cwip_ratio > 10:
            ev.append(f"CWIP {cwip_ratio:.0f}% of net block - build in progress")
    # commissioning: CWIP drained into fixed assets
    if cwip is not None and cwip_prev and cwip_prev > 0 and fa:
        drain = (cwip_prev - cwip) / cwip_prev * 100
        if drain > 30 and cwip_prev / fa > 0.05:
            s += 22
            ev.append(f"CWIP drawn down {drain:.0f}% YoY - capacity commissioned")
    # capacity ahead of revenue = latent operating leverage
    if m.get("revenue_cagr_3y_pct") is not None and fa:
        s += _pos(m.get("q_revenue_yoy_pct"), 8, 35, 16)
    # margin must have room to expand
    vs_peak = m.get("opm_vs_peak_pp")
    if vs_peak is not None and vs_peak < -1:
        s += 12
        ev.append(f"OPM {abs(vs_peak):.1f}pp below 5y peak - absorption yet to come")
    de = m.get("gross_debt_to_equity")
    if de is not None:
        s += 10 if de < 1.0 else (4 if de < 1.8 else 0)
        if de > 2.5:
            s *= 0.55
    ic = m.get("interest_cover_x")
    if ic is not None and ic < 2:
        s *= 0.5
    if not ev:
        return 0, []
    return s, ev


def fit_cyclical_recovery(m):
    if m.get("is_financial"):
        return 0, []
    ev = []
    s = 0.0
    vs_peak = m.get("opm_vs_peak_pp")
    pat_vs_peak = m.get("pat_vs_peak_5y_pct")
    qd = m.get("q_opm_yoy_delta_pp")
    qp = m.get("q_profit_yoy_pct")
    # Must be demonstrably below normal. The bound is exclusive: a name exactly
    # 2pp off its peak is not yet evidence of a trough, and QC check 4 flags
    # anything >= -2, so gate and check must use the same inequality.
    if vs_peak is None or vs_peak >= -2:
        return 0, []
    s += _pos(-vs_peak, 2, 12, 26)
    ev.append(f"OPM {abs(vs_peak):.1f}pp below 5y peak")
    if pat_vs_peak is not None and pat_vs_peak < -20:
        s += _pos(-pat_vs_peak, 20, 70, 18)
        ev.append(f"PAT {abs(pat_vs_peak):.0f}% below 5y peak")
    # early improvement is what separates recovery from decline
    if qd is not None and qd > 0.5:
        s += _pos(qd, 0.5, 6, 22)
        ev.append(f"quarterly OPM +{qd:.1f}pp YoY - margin inflecting")
    if qp is not None and qp > 10:
        s += _pos(qp, 10, 60, 14)
    # survivability
    de = m.get("gross_debt_to_equity")
    ic = m.get("interest_cover_x")
    s += 12 if (de is not None and de < 1.0) else (5 if (de is not None and de < 2.0) else 0)
    if ic is not None and ic < 1.5:
        s *= 0.5
    if de is not None and de > 2.5:
        s *= 0.6
    if qd is None or qd <= 0:
        s *= 0.55   # trough with no evidence of turning yet
    return s, ev


def fit_turnaround(m):
    ev = []
    s = 0.0
    pat = m.get("net_profit_latest_inr_cr")
    qp = m.get("q_profit_yoy_pct")
    opm = m.get("opm_latest_pct")
    qd = m.get("q_opm_yoy_delta_pp")
    roce = m.get("roce_latest_pct")
    # a turnaround starts from actual distress
    distressed = ((pat is not None and pat < 0) or (opm is not None and opm < 4)
                  or (roce is not None and roce < 5))
    if not distressed:
        return 0, []
    ev.append("loss-making or sub-scale margin base")
    if qd is not None and qd > 1:
        s += _pos(qd, 1, 10, 30)
        ev.append(f"quarterly OPM +{qd:.1f}pp YoY")
    if qp is not None and qp > 0:
        s += 18
        ev.append("quarterly profit improving YoY")
    if m.get("q_revenue_yoy_pct") is not None and m["q_revenue_yoy_pct"] > 5:
        s += 12
    de = m.get("gross_debt_to_equity")
    if de is not None:
        s += 16 if de < 1.0 else (8 if de < 2.0 else 0)
        if de > 3:
            s *= 0.5
    cp = m.get("cfo_pat_3y")
    if cp is not None and cp > 1:
        s += 10
    if not (qd or qp):
        return 0, []       # narrative-only turnaround: reject at discovery
    return s, ev


def fit_asset_value(m):
    ev = []
    s = 0.0
    inv = m.get("investments_inr_cr")
    mc = m.get("market_cap_inr_cr")
    if not inv or not mc or mc <= 0:
        return 0, []
    ratio = inv / mc * 100
    if ratio < 25:
        return 0, []
    s += _pos(ratio, 25, 120, 45)
    ev.append(f"balance-sheet investments = {ratio:.0f}% of market cap")
    if m.get("stock_pe") is not None and m["stock_pe"] < 15:
        s += 12
    de = m.get("gross_debt_to_equity")
    s += 15 if (de is not None and de < 0.4) else 0
    bv, px = m.get("book_value_inr"), m.get("current_price_inr")
    if bv and px and px < bv:
        s += 18
        ev.append(f"trades at {px/bv:.2f}x book")
    if m.get("dividend_yield_pct"):
        s += _pos(m["dividend_yield_pct"], 1, 4, 10)
    return s, ev


def fit_financial_compounder(m):
    if not m.get("is_financial"):
        return 0, []
    ev = []
    s = 0.0
    roe = m.get("roe_latest_pct") or m.get("screener_roe_5y")
    s += _pos(roe, 10, 20, 32)
    s += _pos(m.get("screener_roe_5y"), 10, 20, 16)
    s += _pos(m.get("revenue_cagr_5y_pct"), 8, 25, 20)
    s += _pos(m.get("reported_eps_cagr_5y_pct"), 8, 25, 22)
    dil = m.get("equity_capital_change_5y_pct")
    s += 10 if (dil is not None and dil < 20) else 0
    if roe:
        ev.append(f"ROE {roe:.1f}%")
    if m.get("reported_eps_cagr_5y_pct"):
        ev.append(f"5y EPS CAGR {m['reported_eps_cagr_5y_pct']:.0f}%")
    if roe is not None and roe < 9:
        s *= 0.5
    return s, ev


def fit_mature_value_yield(m):
    ev = []
    s = 0.0
    dy = m.get("dividend_yield_pct")
    if not dy or dy < 1.5:
        return 0, []
    s += _pos(dy, 1.5, 6, 32)
    ev.append(f"dividend yield {dy:.1f}%")
    cp = m.get("cfo_pat_5y")
    s += _pos(cp, 0.7, 1.3, 22)
    fp = m.get("fcf_positive_years_5y")
    if fp is not None:
        s += _pos(fp, 2, 5, 16)
        if fp >= 4:
            ev.append(f"FCF positive in {fp} of last 5 years")
    de = m.get("gross_debt_to_equity")
    s += 14 if (de is not None and de < 0.4) else (6 if (de is not None and de < 0.8) else 0)
    roce = m.get("roce_median_5y_pct")
    s += _pos(roce, 10, 22, 16)
    # structural decline guard
    r5 = m.get("revenue_cagr_5y_pct")
    if r5 is not None and r5 < 0:
        s *= 0.5
        ev.append("revenue shrinking - decline risk")
    return s, ev


def fit_speculative(m):
    ev = []
    s = 0.0
    pat = m.get("net_profit_latest_inr_cr")
    r3 = m.get("revenue_cagr_3y_pct")
    mc = m.get("market_cap_inr_cr")
    if pat is None or pat >= 0:
        return 0, []
    if r3 is None or r3 < 15:
        return 0, []
    s += _pos(r3, 15, 60, 40)
    ev.append(f"loss-making with 3y revenue CAGR {r3:.0f}%")
    de = m.get("gross_debt_to_equity")
    s += 20 if (de is not None and de < 0.5) else 0
    if m.get("q_opm_yoy_delta_pp") is not None and m["q_opm_yoy_delta_pp"] > 2:
        s += 20
        ev.append("margin trend improving")
    if mc and mc < 5000:
        s += 10
    return s, ev


VALUE_UNLOCK_EVENTS = {
    "Demerger / scheme of arrangement": 42,
    "Subsidiary listing": 40,
    "Capital reduction": 32,
    "Asset / business sale": 30,
    "Delisting": 38,
    "Open offer / control change": 34,
    "Buyback": 22,
}
NEGATIVE_EVENTS = {"Insolvency / resolution", "Auditor change/qualification",
                   "Regulatory action", "Equity raise"}


def fit_event_driven(m, events: list[dict] | None = None):
    """
    Requires a disclosed corporate event capable of re-rating the equity.
    Financial statements alone cannot support this archetype, so with no
    announcement evidence the fit is zero by construction.
    """
    if not events:
        return 0, []
    ev, s = [], 0.0
    for e in events:
        cls = e.get("event_class")
        if cls in VALUE_UNLOCK_EVENTS:
            s += VALUE_UNLOCK_EVENTS[cls]
            ev.append(f"{cls} disclosed {e.get('latest_date', '')}".strip())
    if s == 0:
        return 0, []
    # downside protection matters more than upside in a special situation
    de = m.get("gross_debt_to_equity")
    if de is not None and de < 1.0:
        s += 10
    if (m.get("cfo_pat_5y") or 0) > 0.8:
        s += 8
    if any(e.get("event_class") in NEGATIVE_EVENTS for e in events):
        s *= 0.8
    return min(s, 100), ev[:3]


ARCHETYPES = [
    ("Quality compounder", fit_quality_compounder),
    ("High-growth company", fit_high_growth),
    ("Capex operating-leverage candidate", fit_capex_leverage),
    ("Cyclical recovery", fit_cyclical_recovery),
    ("Turnaround", fit_turnaround),
    ("Asset-value/SOTP opportunity", fit_asset_value),
    ("Financial compounder", fit_financial_compounder),
    ("Mature value/yield company", fit_mature_value_yield),
    ("Speculative/emerging business", fit_speculative),
]


def classify(m: dict, events: list[dict] | None = None) -> dict:
    scores = {}
    evidence = {}
    for name, fn in ARCHETYPES:
        try:
            s, ev = fn(m)
        except Exception:
            s, ev = 0, []
        scores[name] = round(float(s), 1)
        evidence[name] = ev
    try:
        s, ev = fit_event_driven(m, events)
    except Exception:
        s, ev = 0, []
    scores["Event-driven or special situation"] = round(float(s), 1)
    evidence["Event-driven or special situation"] = ev
    best = max(scores, key=lambda k: scores[k])
    if scores[best] <= 0:
        return {"primary_archetype": None, "archetype_fit": 0.0,
                "classification_rationale": "no archetype test passed on available data",
                "archetype_scores": scores}
    ev = evidence[best]
    return {
        "primary_archetype": best,
        "archetype_fit": scores[best],
        "classification_rationale": ("; ".join(ev)[:300] if ev else "best-fitting archetype on screened metrics"),
        "archetype_scores": scores,
        "runner_up": sorted(scores, key=lambda k: -scores[k])[1],
    }


def secondary_tags(m: dict, tech: dict, arche: str | None,
                   events: list[dict] | None = None) -> list[str]:
    """Controlled vocabulary only. 'Undervalued' is never assigned in Phase 1."""
    t = []
    for e in (events or []):
        cls = e.get("event_class")
        if cls in ("Demerger / scheme of arrangement", "Subsidiary listing",
                   "Capital reduction", "Asset / business sale"):
            t.append("Demerger/SOTP unlocking")
        elif cls in ("Open offer / control change", "Delisting", "Buyback"):
            t.append("Regulatory catalyst")
        elif cls in ("Insolvency / resolution", "Auditor change/qualification",
                     "Regulatory action"):
            t.append("Governance risk")
        elif cls == "Equity raise":
            t.append("Governance risk")
    qd = m.get("q_opm_yoy_delta_pp")
    qp = m.get("q_profit_yoy_pct")
    qr = m.get("q_revenue_yoy_pct")

    if qp is not None and qp > 25 and qr is not None and qr > 10:
        t.append("Earnings-upgrade candidate")
    cwr = m.get("cwip_to_gross_block_pct")
    if cwr is not None and cwr > 12:
        t.append("Capex commissioning")
    if qd is not None and qd > 2:
        t.append("Operating-leverage inflection")
    vsp = m.get("opm_vs_peak_pp")
    if vsp is not None and vsp < -2 and qd is not None and qd > 0:
        t.append("Margin-recovery candidate")
    if (qp is not None and qp > 20) and (tech.get("rs_bm_13w_pct") or 0) > 5:
        t.append("PEAD candidate")
    de = m.get("gross_debt_to_equity")
    if de is not None and de > 0.4 and (m.get("cfo_pat_5y") or 0) > 1.0:
        t.append("Debt-reduction story")
    inv = m.get("investments_inr_cr")
    mc = m.get("market_cap_inr_cr")
    if inv and mc and inv / mc > 0.4:
        t.append("Demerger/SOTP unlocking")

    # risk tags
    if at_or_near_peak_margin(m) and (is_cyclical(m) or (vsp is not None and vsp > -0.5)):
        t.append("Peak-cycle risk")
    prom = m.get("promoter_holding_pct")
    pch = m.get("promoter_change_pct")
    if pch is not None and pch < -3:
        t.append("Governance risk")
    dil = m.get("equity_capital_change_5y_pct")
    if dil is not None and dil > 40:
        t.append("Governance risk")
    cp = m.get("cfo_pat_5y")
    if cp is not None and not m.get("is_financial") and cp < 0.5:
        t.append("Value-trap risk")
    pe = m.get("stock_pe")
    if pe is not None and pe > 60 and arche == "Quality compounder":
        t.append("Overvalued quality")
    if pe is not None and 0 < pe < 12 and (m.get("revenue_cagr_5y_pct") or 0) < 6:
        t.append("Statistically cheap-catalyst unclear")
    if m.get("earnings_quality_flag"):
        t.append("Temporary setback" if (qd or 0) > 0 else "Value-trap risk")

    seen, outl = set(), []
    for x in t:
        if x not in seen:
            seen.add(x)
            outl.append(x)
    return outl
