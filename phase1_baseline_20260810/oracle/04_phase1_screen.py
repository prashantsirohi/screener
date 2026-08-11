"""
Phase 1 - eligibility, three-axis classification, preliminary priority score,
candidate selection, and the five hand-off artefacts.

Run after 01/02/03 have populated data/.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from metrics import compute            # noqa: E402
from weinstein import load_bars, analyse  # noqa: E402
from archetypes import classify, secondary_tags  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
FUND = ROOT / "data" / "fundamentals"
PRICES = ROOT / "data" / "prices"
OUT = ROOT / "phase1"
OUT.mkdir(parents=True, exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))
STARTED = datetime.now(IST)

MCAP_MIN, MCAP_MAX = 1000.0, 100000.0
LIQ_MIN_CR = 1.0          # median daily traded value floor, INR crore
MIN_FY = 3                # minimum years of annual financial history
MIN_WEEKS_PRICE = 40

# Sector index mapping for relative-strength comparison
SECTOR_BM = {
    "Information Technology": "NIFTY_IT",
    "Financial Services": "NIFTY_BANK",
    "Healthcare": "NIFTY_PHARMA",
    "Automobile and Auto Components": "NIFTY_AUTO",
    "Fast Moving Consumer Goods": "NIFTY_FMCG",
    "Metals & Mining": "NIFTY_METAL",
    "Oil Gas & Consumable Fuels": "NIFTY_ENERGY",
    "Power": "NIFTY_ENERGY",
    "Construction": "NIFTY_INFRA",
    "Realty": "NIFTY_REALTY",
    "Capital Goods": "NIFTY_INFRA",
}

EXCLUSION_CODES = {
    "EX_NO_FUNDAMENTALS": "No usable financial record retrievable for the symbol",
    "EX_NO_MCAP": "Market capitalisation not available from any source",
    "EX_MCAP_BELOW_BAND": "Market cap below INR 1,000 cr floor",
    "EX_MCAP_ABOVE_BAND": "Market cap above INR 1,00,000 cr ceiling",
    "EX_ILLIQUID": f"Median daily traded value below INR {LIQ_MIN_CR} cr",
    "EX_SHORT_FIN_HISTORY": f"Fewer than {MIN_FY} years of annual financial history",
    "EX_NO_PRICE_HISTORY": f"Fewer than {MIN_WEEKS_PRICE} adjusted weekly bars",
    "EX_NO_ARCHETYPE": "No archetype discovery test passed on available data",
    "EX_DATA_QUALITY": "Core screening metrics missing - cannot classify reliably",
}


def liquidity_from_bars(df: pd.DataFrame) -> tuple[float | None, str]:
    """13-week median DAILY traded value in INR crore, derived from weekly bars."""
    if df is None or len(df) < 13:
        return None, "insufficient bars"
    d = df.tail(13)
    typical = d[["high", "low", "adjclose"]].mean(axis=1)
    weekly_val = (typical * d["volume"]).dropna()
    if weekly_val.empty:
        return None, "no volume"
    daily_cr = float(np.median(weekly_val)) / 5.0 / 1e7
    return round(daily_cr, 3), "13-week median of weekly traded value / 5"


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


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def main() -> int:
    frame = pd.read_csv(RAW / "universe_frame.csv")
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    bhav_date = str(frame["price_date"].dropna().iloc[0]) if "price_date" in frame.columns \
        and frame["price_date"].notna().any() else None

    # corporate events (NSE announcements) - the only source that can support the
    # event-driven archetype and disclosed governance flags
    events_by_sym: dict[str, list[dict]] = {}
    ef_path = RAW / "event_flags.csv"
    if ef_path.exists():
        ef = pd.read_csv(ef_path)
        for r in ef.itertuples(index=False):
            events_by_sym.setdefault(str(r.symbol).strip(), []).append({
                "event_class": r.event_class, "event_tag": r.event_tag,
                "latest_date": str(r.latest_date), "headline": str(r.headline)})
        print(f"Event flags loaded for {len(events_by_sym)} symbols")
    else:
        print("WARNING: no event_flags.csv - event-driven archetype under-detected")

    benches = {}
    for p in PRICES.glob("_BM_*.json"):
        benches[p.stem.replace("_BM_", "")] = load_bars(p)
    bm500 = benches.get("NIFTY_500")
    if bm500 is None:
        bm500 = benches.get("NIFTY_50")
    print(f"Benchmarks loaded: {len(benches)}  (broad = NIFTY_500, "
          f"{len(bm500) if bm500 is not None else 0} bars)")

    rows = []
    n = len(frame)
    for i, r in enumerate(frame.itertuples(index=False), 1):
        sym = r.symbol
        rec_p = FUND / f"{sym}.json"
        px_p = PRICES / f"{sym}.json"

        rec = json.loads(rec_p.read_text(encoding="utf-8")) if rec_p.exists() else {"error": "NOT_FETCHED"}
        industry = getattr(r, "industry", None)
        industry = None if (isinstance(industry, float) and pd.isna(industry)) else industry
        m = compute(rec, industry)

        bars = load_bars(px_p) if px_p.exists() else None
        sector_bm = benches.get(SECTOR_BM.get(industry or "", ""), None)
        tech = analyse(bars, bm500, sector_bm) if bars is not None else {
            "technical_stage": "Indeterminate-insufficient adjusted history",
            "stage_rationale": "no adjusted weekly price history retrieved"}
        liq, liq_desc = liquidity_from_bars(bars)

        # ---------------- market cap ----------------
        # screener's live value where the page served it; otherwise derive it from
        # primary data: NSE close x (equity capital / face value). The fallback is
        # only trusted when the balance sheet is recent, since an older share count
        # misses subsequent issuance, splits and bonuses.
        mc = m.get("market_cap_inr_cr")
        mc_method = "Aggregator reported" if mc is not None else None
        if mc is None:
            eqc = m.get("equity_capital_latest_inr_cr")
            face = getattr(r, "face_value", None)
            close = getattr(r, "close_inr", None)
            bs_period = m.get("balance_sheet_latest_period") or ""
            recent_bs = bs_period.endswith(("2025", "2026"))
            if eqc and face and close and face > 0 and recent_bs:
                mc = close * (eqc / face)
                mc_method = f"Computed: NSE close x (equity capital / face value), BS {bs_period}"
        if mc is None:
            mc_method = "Unavailable"
        m["market_cap_inr_cr"] = mc   # archetype tests read it from the metric bundle

        # ---------------- eligibility ----------------
        excl, reason = None, None
        if m.get("data_error") or not m.get("company"):
            excl, reason = "EX_NO_FUNDAMENTALS", EXCLUSION_CODES["EX_NO_FUNDAMENTALS"]
        elif mc is None:
            excl, reason = "EX_NO_MCAP", EXCLUSION_CODES["EX_NO_MCAP"]
        elif mc < MCAP_MIN:
            excl, reason = "EX_MCAP_BELOW_BAND", f"Market cap INR {mc:,.0f} cr < 1,000 cr"
        elif mc > MCAP_MAX:
            excl, reason = "EX_MCAP_ABOVE_BAND", f"Market cap INR {mc:,.0f} cr > 1,00,000 cr"
        elif (m.get("fy_count") or 0) < MIN_FY:
            excl, reason = "EX_SHORT_FIN_HISTORY", f"only {m.get('fy_count')} annual periods"
        elif bars is None or len(bars) < MIN_WEEKS_PRICE:
            excl, reason = "EX_NO_PRICE_HISTORY", EXCLUSION_CODES["EX_NO_PRICE_HISTORY"]
        elif liq is None or liq < LIQ_MIN_CR:
            excl, reason = "EX_ILLIQUID", (f"median daily traded value INR {liq} cr"
                                           if liq is not None else "no volume data")

        cls = {"primary_archetype": None, "archetype_fit": 0.0,
               "classification_rationale": None}
        tags, score, breakdown = [], None, {}
        evs = events_by_sym.get(sym, [])
        if excl is None:
            cls = classify(m, evs)
            if not cls["primary_archetype"]:
                excl, reason = "EX_NO_ARCHETYPE", EXCLUSION_CODES["EX_NO_ARCHETYPE"]
            else:
                tags = secondary_tags(m, tech, cls["primary_archetype"], evs)
                score, breakdown = score_priority(m, tech, cls["archetype_fit"], liq)

        # data-quality confidence
        core = [m.get("revenue_cagr_5y_pct"), m.get("roce_median_5y_pct"),
                m.get("cfo_pat_5y"), m.get("net_debt_to_equity"), m.get("promoter_holding_pct")]
        filled = sum(1 for x in core if x is not None)
        dq = "High" if filled >= 5 else ("Medium" if filled >= 3 else "Low")
        if m.get("earnings_quality_flag"):
            dq = "Medium" if dq == "High" else "Low"

        fin = bool(m.get("is_financial"))
        val_method = ("P/B vs ROE (preliminary)" if fin else
                      "Trailing P/E vs 3-5y EPS CAGR (preliminary)")
        val_value = m.get("stock_pe")

        risk = None
        if (m.get("cfo_pat_5y") is not None and not fin and m["cfo_pat_5y"] < 0.6):
            risk = "Weak cash conversion (cumulative CFO/PAT below 0.6)"
        elif (m.get("net_debt_to_equity") or 0) > 1.8:
            risk = f"Leverage: debt/equity {m.get('net_debt_to_equity'):.2f}"
        elif (m.get("promoter_change_pct") or 0) < -3:
            risk = f"Promoter stake down {abs(m['promoter_change_pct']):.1f}pp over 4 quarters"
        elif (m.get("equity_capital_change_5y_pct") or 0) > 40:
            risk = f"Equity capital up {m['equity_capital_change_5y_pct']:.0f}% in 5y - dilution"
        elif m.get("earnings_quality_flag"):
            risk = m["earnings_quality_flag"]
        elif tech.get("technical_stage") in ("Stage 4 decline", "Stage 3 distribution"):
            risk = f"Technically in {tech['technical_stage']}"
        else:
            risk = "None identified at Phase 1 depth"

        rows.append({
            "screening_date": STARTED.date().isoformat(),
            "company": m.get("company") or getattr(r, "company", None),
            "symbol": sym,
            "exchange": "NSE",
            "listing_board": "Main Board",
            "sector": industry or "Not disclosed",
            "industry": industry or "Not disclosed",
            "current_price_inr": m.get("current_price_inr") or getattr(r, "close_inr", None),
            "price_date": bhav_date,
            "market_cap_inr_cr": mc,
            "market_cap_date": bhav_date,
            "liquidity_metric_name": "3-month median daily traded value",
            "liquidity_value_inr_cr": liq,
            "liquidity_period": liq_desc,
            "eligible_flag": int(excl is None),
            "exclusion_code": excl or "",
            "exclusion_reason": reason or "",
            "primary_archetype": cls.get("primary_archetype") or "",
            "classification_rationale": cls.get("classification_rationale") or "",
            "secondary_tags": "|".join(tags),
            "technical_stage": tech.get("technical_stage"),
            "technical_data_date": tech.get("technical_data_date") or "",
            "revenue_cagr_5y_pct": m.get("revenue_cagr_5y_pct"),
            "normalized_eps_cagr_5y_pct": m.get("eps_cagr_5y_pct"),
            "latest_roce_or_roe_pct": m.get("roe_latest_pct") if fin else m.get("roce_latest_pct"),
            "median_roce_or_roe_5y_pct": m.get("screener_roe_5y") if fin else m.get("roce_median_5y_pct"),
            "net_debt_to_equity": m.get("net_debt_to_equity"),
            "cfo_pat_period": m.get("cfo_pat_period") or "",
            "cfo_pat_ratio": m.get("cfo_pat_5y") if not fin else None,
            "preliminary_valuation_method": val_method,
            "preliminary_valuation_value": val_value,
            "preliminary_priority_score": score,
            "inclusion_reason": cls.get("classification_rationale") or "",
            "key_disqualifying_risk": risk,
            "data_quality_confidence": dq,
            "primary_source_ids": "NSE-EQL-2026-08-10|NSE-BHAV-2026-08-10",
            "secondary_source_ids": f"{sym}-SCR-01|{sym}-PXW-01",
            # internal, dropped before write
            "_fit": cls.get("archetype_fit"),
            "_runner_up": cls.get("runner_up"),
            "_stage_rationale": tech.get("stage_rationale"),
            "_pivot": tech.get("pivot_level"),
            "_rs13": tech.get("rs_bm_13w_pct"),
            "_rs52": tech.get("rs_bm_52w_pct"),
            "_dist_ma30": tech.get("distance_from_ma30w_pct"),
            "_breakdown": json.dumps(breakdown),
            "_is_financial": fin,
            "_fy_count": m.get("fy_count"),
            "_latest_fy": m.get("latest_fy"),
            "_latest_q": m.get("latest_quarter"),
            "_basis": m.get("basis"),
            "_src_url": m.get("source_url"),
            "_mcap_method": mc_method,
            "_events": "|".join(sorted({e["event_class"] for e in evs})),
            "_event_latest": max((e["latest_date"] for e in evs), default=""),
            "_cwip_ratio": m.get("cwip_to_gross_block_pct"),
            "_q_rev_yoy": m.get("q_revenue_yoy_pct"),
            "_q_pat_yoy": m.get("q_profit_yoy_pct"),
            "_q_opm_delta": m.get("q_opm_yoy_delta_pp"),
            "_opm_vs_peak": m.get("opm_vs_peak_pp"),
            "_promoter": m.get("promoter_holding_pct"),
            "_promoter_chg": m.get("promoter_change_pct"),
            "_dilution": m.get("equity_capital_change_5y_pct"),
            "_pe": m.get("stock_pe"),
            "_pb": (m.get("current_price_inr") / m.get("book_value_inr")
                    if m.get("current_price_inr") and m.get("book_value_inr") else None),
            "_dy": m.get("dividend_yield_pct"),
            "_eq_flag": m.get("earnings_quality_flag"),
            "_base_weeks": tech.get("base_duration_weeks"),
            "_support": tech.get("support_level_inr"),
            "_invalidation": tech.get("invalidation_level_inr"),
            "_extension": tech.get("entry_extension_verdict"),
            "_overhead": tech.get("overhead_supply_verdict"),
            "_wc_days": m.get("working_capital_days"),
            "_int_cover": m.get("interest_cover_x"),
        })
        if i % 250 == 0 or i == n:
            print(f"  screened {i}/{n}", flush=True)

    df = pd.DataFrame(rows)
    df.to_pickle(ROOT / "data" / "phase1_full.pkl")
    print(f"\nScreened {len(df)} securities")
    print(f"Eligible: {int(df['eligible_flag'].sum())}")
    print("\nExclusion breakdown:")
    print(df[df["eligible_flag"] == 0]["exclusion_code"].value_counts().to_string())
    print("\nArchetype distribution (eligible):")
    print(df[df["eligible_flag"] == 1]["primary_archetype"].value_counts().to_string())
    print("\nTechnical stage (eligible):")
    print(df[df["eligible_flag"] == 1]["technical_stage"].value_counts().to_string())
    el = df[df["eligible_flag"] == 1]
    if len(el):
        print("\nPriority score distribution (eligible):")
        print(el["preliminary_priority_score"].describe().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
