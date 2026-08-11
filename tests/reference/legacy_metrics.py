"""
Derive Phase 1 screening metrics from cached screener.in company records.

Everything here is arithmetic on the scraped statements. Where a figure cannot
be computed from available data it is returned as None and later written as
"Not disclosed" - never guessed.

Normalisation caveat: screener's reported EPS/PAT include exceptional items.
True normalisation needs the filings themselves, which is Phase 2 work. At
Phase 1 we compute reported figures, flag names where other income or a tax
anomaly materially distorts PBT, and mark the metric provisional.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np

FY_RE = re.compile(r"^(Mar|Jun|Sep|Dec)\s+(\d{4})$")


def fy_cols(d: dict) -> list[str]:
    """Ordered fiscal-year column labels present in a statement block."""
    cols = [c for c in d.keys() if FY_RE.match(c)]
    return sorted(cols, key=lambda c: int(FY_RE.match(c).group(2)))


def series(block: dict, row: str) -> tuple[list[str], list[float | None]]:
    r = block.get(row)
    if not r:
        return [], []
    cols = fy_cols(r)
    return cols, [r.get(c) for c in cols]


def cagr(first: float | None, last: float | None, years: int) -> float | None:
    """CAGR in %, only defined when the base is positive."""
    if first is None or last is None or years <= 0:
        return None
    if first <= 0 or last <= 0:
        return None
    try:
        return (( last / first) ** (1.0 / years) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def safe_div(a, b):
    if a is None or b is None:
        return None
    try:
        if b == 0 or (isinstance(b, float) and math.isnan(b)):
            return None
        return a / b
    except Exception:
        return None


def med(vals) -> float | None:
    v = [x for x in vals if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return float(np.median(v)) if v else None


def _sum_last(vals: list, n: int) -> float | None:
    v = [x for x in vals[-n:] if x is not None]
    return float(sum(v)) if len(v) == min(n, len([x for x in vals if x is not None])) and v else (
        float(sum(v)) if v else None)


def is_financial(rec: dict, industry: str | None) -> bool:
    pl = rec.get("profit_loss") or {}
    if "Financing Profit" in pl or "Financing Margin %" in pl:
        return True
    ind = (industry or "").lower()
    return any(k in ind for k in ("financial service", "bank", "insurance"))


def compute(rec: dict, industry: str | None = None) -> dict:
    """Return the Phase 1 metric bundle for one company."""
    out: dict = {"symbol": rec.get("symbol")}
    if rec.get("error"):
        out["data_error"] = rec["error"]
        return out

    tr = rec.get("top_ratios") or {}
    pl = rec.get("profit_loss") or {}
    bs = rec.get("balance_sheet") or {}
    cf = rec.get("cash_flow") or {}
    rt = rec.get("ratios") or {}
    sh = rec.get("shareholding") or {}
    gr = rec.get("growth") or {}

    fin = is_financial(rec, industry)
    out["is_financial"] = fin
    out["industry"] = industry
    out["basis"] = rec.get("basis")
    out["company"] = rec.get("name")
    out["market_cap_inr_cr"] = tr.get("Market Cap")
    out["current_price_inr"] = tr.get("Current Price")
    out["stock_pe"] = tr.get("Stock P/E")
    out["book_value_inr"] = tr.get("Book Value")
    out["dividend_yield_pct"] = tr.get("Dividend Yield")
    out["face_value_inr"] = tr.get("Face Value")
    out["roce_latest_pct"] = tr.get("ROCE")
    out["roe_latest_pct"] = tr.get("ROE")
    out["high_52w_screener"] = tr.get("high_52w")
    out["low_52w_screener"] = tr.get("low_52w")

    # ---------- revenue / earnings ----------
    rev_row = "Sales" if "Sales" in pl else ("Revenue" if "Revenue" in pl else None)
    rcols, rvals = series(pl, rev_row) if rev_row else ([], [])
    ecols, evals = series(pl, "EPS in Rs")
    ncols, nvals = series(pl, "Net Profit")
    opm_cols, opm = series(pl, "OPM %") if "OPM %" in pl else series(pl, "Financing Margin %")
    oi_cols, oi = series(pl, "Other Income")
    pbt_cols, pbt = series(pl, "Profit before tax")
    dep_cols, dep = series(pl, "Depreciation")
    int_cols, intr = series(pl, "Interest")

    out["latest_fy"] = rcols[-1] if rcols else None
    out["fy_count"] = len(rcols)
    out["revenue_latest_inr_cr"] = rvals[-1] if rvals else None
    out["revenue_ttm_inr_cr"] = (pl.get(rev_row) or {}).get("TTM") if rev_row else None
    out["net_profit_latest_inr_cr"] = nvals[-1] if nvals else None
    out["eps_latest_inr"] = evals[-1] if evals else None
    out["eps_ttm_inr"] = (pl.get("EPS in Rs") or {}).get("TTM")
    out["opm_latest_pct"] = opm[-1] if opm else None
    out["opm_median_5y_pct"] = med(opm[-5:]) if opm else None

    if len(rvals) >= 6:
        out["revenue_cagr_5y_pct"] = cagr(rvals[-6], rvals[-1], 5)
    if len(rvals) >= 4:
        out["revenue_cagr_3y_pct"] = cagr(rvals[-4], rvals[-1], 3)
    if len(evals) >= 6:
        out["eps_cagr_5y_pct"] = cagr(evals[-6], evals[-1], 5)
    if len(evals) >= 4:
        out["eps_cagr_3y_pct"] = cagr(evals[-4], evals[-1], 3)

    # screener's own compounded growth tables (cross-check)
    out["screener_sales_cagr_5y"] = (gr.get("Compounded Sales Growth") or {}).get("5 Years")
    out["screener_profit_cagr_5y"] = (gr.get("Compounded Profit Growth") or {}).get("5 Years")
    out["screener_roe_5y"] = (gr.get("Return on Equity") or {}).get("5 Years")
    out["stock_cagr_5y"] = (gr.get("Stock Price CAGR") or {}).get("5 Years")

    # ---------- returns ----------
    rc_cols, roce_hist = series(rt, "ROCE %")
    out["roce_median_5y_pct"] = med(roce_hist[-5:]) if roce_hist else None
    out["roce_median_3y_pct"] = med(roce_hist[-3:]) if roce_hist else None

    # ---------- balance sheet ----------
    _, borrow = series(bs, "Borrowings")
    _, reserves = series(bs, "Reserves")
    _, eqcap = series(bs, "Equity Capital")
    _, cwip = series(bs, "CWIP")
    _, fixed = series(bs, "Fixed Assets")
    _, invest = series(bs, "Investments")
    _, totassets = series(bs, "Total Assets")

    equity = None
    if reserves and eqcap and reserves[-1] is not None and eqcap[-1] is not None:
        equity = reserves[-1] + eqcap[-1]
    out["equity_inr_cr"] = equity
    out["equity_capital_latest_inr_cr"] = eqcap[-1] if eqcap else None
    bs_cols = fy_cols(bs.get("Equity Capital", {})) if bs.get("Equity Capital") else []
    out["balance_sheet_latest_period"] = bs_cols[-1] if bs_cols else None
    out["borrowings_inr_cr"] = borrow[-1] if borrow else None
    out["cwip_inr_cr"] = cwip[-1] if cwip else None
    out["cwip_prev_inr_cr"] = cwip[-2] if len(cwip) > 1 else None
    out["fixed_assets_inr_cr"] = fixed[-1] if fixed else None
    out["investments_inr_cr"] = invest[-1] if invest else None
    out["total_assets_inr_cr"] = totassets[-1] if totassets else None

    if not fin:
        out["debt_to_equity"] = safe_div(out["borrowings_inr_cr"], equity)
        # screener does not expose cash separately; borrowings/equity is the usable proxy
        out["net_debt_to_equity"] = out["debt_to_equity"]
    else:
        out["debt_to_equity"] = None
        out["net_debt_to_equity"] = None

    # CWIP intensity - a capex-commissioning tell
    out["cwip_to_gross_block_pct"] = (
        safe_div(out["cwip_inr_cr"], out["fixed_assets_inr_cr"]) or 0) * 100 \
        if out["cwip_inr_cr"] is not None and out["fixed_assets_inr_cr"] else None
    if out["cwip_inr_cr"] is not None and out["cwip_prev_inr_cr"]:
        out["cwip_change_pct"] = (out["cwip_inr_cr"] / out["cwip_prev_inr_cr"] - 1) * 100 \
            if out["cwip_prev_inr_cr"] else None

    # ---------- cash flow ----------
    _, cfo = series(cf, "Cash from Operating Activity")
    _, fcf = series(cf, "Free Cash Flow")
    out["cfo_latest_inr_cr"] = cfo[-1] if cfo else None
    for n in (3, 5):
        c = [x for x in cfo[-n:] if x is not None]
        p = [x for x in nvals[-n:] if x is not None]
        if len(c) == n and len(p) == n and sum(p) != 0:
            out[f"cfo_pat_{n}y"] = sum(c) / sum(p)
    out["cfo_pat_period"] = (f"{rcols[-5]}-{rcols[-1]}"
                             if len(rcols) >= 5 and out.get("cfo_pat_5y") is not None
                             else (f"{rcols[-3]}-{rcols[-1]}"
                                   if len(rcols) >= 3 and out.get("cfo_pat_3y") is not None else None))
    out["fcf_latest_inr_cr"] = fcf[-1] if fcf else None
    fpos = [x for x in fcf[-5:] if x is not None]
    out["fcf_positive_years_5y"] = sum(1 for x in fpos if x > 0) if fpos else None

    # ---------- working capital ----------
    for label, key in (("Working Capital Days", "working_capital_days"),
                       ("Debtor Days", "debtor_days"),
                       ("Inventory Days", "inventory_days"),
                       ("Cash Conversion Cycle", "cash_conversion_cycle")):
        _, v = series(rt, label)
        out[key] = v[-1] if v else None
        out[f"{key}_prev"] = v[-2] if len(v) > 1 else None

    # ---------- shareholding ----------
    _, prom = series(sh, "Promoters")
    _, fii = series(sh, "FIIs")
    _, dii = series(sh, "DIIs")
    out["promoter_holding_pct"] = prom[-1] if prom else None
    out["promoter_holding_4q_ago"] = prom[-5] if len(prom) >= 5 else None
    out["promoter_change_pct"] = (
        out["promoter_holding_pct"] - out["promoter_holding_4q_ago"]
        if out["promoter_holding_pct"] is not None and out["promoter_holding_4q_ago"] is not None
        else None)
    out["fii_holding_pct"] = fii[-1] if fii else None
    out["dii_holding_pct"] = dii[-1] if dii else None
    out["free_float_pct"] = (100 - out["promoter_holding_pct"]
                             if out["promoter_holding_pct"] is not None else None)

    # ---------- dilution ----------
    if len(eqcap) >= 6 and eqcap[-6] and eqcap[-1]:
        out["equity_capital_change_5y_pct"] = (eqcap[-1] / eqcap[-6] - 1) * 100

    # ---------- quality flags ----------
    if oi and pbt and oi[-1] is not None and pbt[-1] not in (None, 0):
        out["other_income_to_pbt_pct"] = oi[-1] / pbt[-1] * 100
    out["earnings_quality_flag"] = None
    oip = out.get("other_income_to_pbt_pct")
    if oip is not None and oip > 35:
        out["earnings_quality_flag"] = "Other income >35% of PBT - EPS provisional"

    # interest cover
    if pbt and intr and pbt[-1] is not None and intr[-1]:
        out["interest_cover_x"] = (pbt[-1] + intr[-1]) / intr[-1]

    # ---------- quarterly momentum (PEAD / recovery tells) ----------
    q_rev_row = "Sales" if "Sales" in (rec.get("quarters") or {}) else "Revenue"
    q = rec.get("quarters") or {}
    qr = q.get(q_rev_row) or {}
    qn = q.get("Net Profit") or {}
    qo = q.get("OPM %") or {}
    qcols = [c for c in qr.keys() if FY_RE.match(c)]
    qcols = sorted(qcols, key=lambda c: (int(FY_RE.match(c).group(2)),
                                         {"Mar": 4, "Jun": 1, "Sep": 2, "Dec": 3}[FY_RE.match(c).group(1)]))
    if len(qcols) >= 5:
        a, b = qr.get(qcols[-1]), qr.get(qcols[-5])
        out["q_revenue_yoy_pct"] = (a / b - 1) * 100 if a and b else None
        a, b = qn.get(qcols[-1]), qn.get(qcols[-5])
        out["q_profit_yoy_pct"] = (a / b - 1) * 100 if a and b and b > 0 else None
        out["q_opm_latest_pct"] = qo.get(qcols[-1])
        out["q_opm_yoy_delta_pp"] = (
            qo.get(qcols[-1]) - qo.get(qcols[-5])
            if qo.get(qcols[-1]) is not None and qo.get(qcols[-5]) is not None else None)
        out["latest_quarter"] = qcols[-1]

    # trough detection for cyclicals: latest OPM vs 5y peak
    if opm:
        o5 = [x for x in opm[-6:] if x is not None]
        if o5 and opm[-1] is not None:
            out["opm_peak_5y_pct"] = max(o5)
            out["opm_vs_peak_pp"] = opm[-1] - max(o5)
    if nvals:
        n5 = [x for x in nvals[-6:] if x is not None]
        if n5 and nvals[-1] is not None and max(n5) > 0:
            out["pat_vs_peak_5y_pct"] = (nvals[-1] / max(n5) - 1) * 100

    out["source_url"] = rec.get("source_url")
    return out
