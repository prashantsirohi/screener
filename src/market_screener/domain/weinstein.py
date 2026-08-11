"""
Weinstein stage analysis computed from split/dividend-adjusted WEEKLY bars.

Stage definitions follow Stan Weinstein, "Secrets for Profiting in Bull and Bear
Markets": the 30-week moving average and its slope define the stage; relative
strength versus the broad market confirms it; volume confirms the transition.

Everything here is derived arithmetically from the adjusted price series - no
stage is assigned by judgement or recollection.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

STAGES = [
    "Stage 4 decline",
    "Early Stage 1",
    "Mature Stage 1 base",
    "Stage 1-to-Stage 2 transition",
    "Early Stage 2",
    "Mature/extended Stage 2",
    "Stage 3 distribution",
    "Indeterminate-insufficient adjusted history",
]

MIN_WEEKS = 40           # below this the 40w MA does not exist at all
FLAT_SLOPE = 1.5         # |13-week % change in MA30| under this counts as "flat"
EXTENDED_ABOVE_MA30 = 22.0   # % above MA30 that makes an entry extended
LONG_STAGE2_WEEKS = 78       # ~18 months already advancing = mature


def bars_from_frame(df: pd.DataFrame) -> pd.DataFrame | None:
    """
    Build the analysis frame from weekly_bar rows.

    Replaces the legacy JSON reader. The store already holds adjusted values -
    the import applied the same adjclose/close scaling to OHL that the legacy
    loader did - so nothing is rescaled here and the numbers are identical.

    Everything below this function is a verbatim port of the frozen oracle, and
    tests/parity/test_technical_parity.py asserts that field for field. The
    formula lives here and only here; no SQL recomputes any of it. DuckDB's job
    is the bulk scan and the benchmark join, not the arithmetic.
    """
    if df is None or df.empty:
        return None
    out = pd.DataFrame({
        "date": pd.to_datetime(df["week_end_date"]),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce"),
    })
    out["adjclose"] = out["close"]
    out = out.dropna(subset=["adjclose"]).reset_index(drop=True)
    if out.empty:
        return None
    out["date"] = out["date"].dt.normalize()
    return out.sort_values("date").reset_index(drop=True)


def _pct(a, b):
    if a is None or b is None or b == 0 or (isinstance(b, float) and math.isnan(b)):
        return None
    try:
        return (a / b - 1.0) * 100.0
    except ZeroDivisionError:
        return None


def _slope_pct(series: pd.Series, lookback: int) -> float | None:
    """% change of a moving average over `lookback` weeks - the MA's direction."""
    s = series.dropna()
    if len(s) < lookback + 1:
        return None
    prev, cur = s.iloc[-(lookback + 1)], s.iloc[-1]
    if prev == 0 or pd.isna(prev) or pd.isna(cur):
        return None
    return float((cur / prev - 1.0) * 100.0)


def find_base(df: pd.DataFrame, max_lookback: int = 130) -> dict:
    """
    Locate the most recent sideways consolidation ending at/near the last bar.

    Walks backwards extending a candidate window while the window stays 'contained'
    (depth under ~35% and the window high not materially exceeded early on). Returns
    the longest such contained window.
    """
    n = len(df)
    if n < 12:
        return {}
    close = df["adjclose"].values
    high = df["high"].fillna(df["adjclose"]).values
    low = df["low"].fillna(df["adjclose"]).values

    best = {}
    end = n - 1
    for start in range(n - 8, max(n - max_lookback, 0) - 1, -1):
        win_h = float(np.nanmax(high[start:end + 1]))
        win_l = float(np.nanmin(low[start:end + 1]))
        if win_h <= 0:
            continue
        depth = (win_h - win_l) / win_h * 100.0
        dur = end - start + 1
        if depth > 30.0:
            break
        best = {
            "base_start_idx": start,
            "base_start_date": df["date"].iloc[start].date().isoformat(),
            "base_end_date": df["date"].iloc[end].date().isoformat(),
            "base_duration_weeks": int(dur),
            "base_depth_pct": round(depth, 2),
            "pivot_level": round(win_h, 2),
            "base_low": round(win_l, 2),
        }
    return best


def weeks_above_ma(close: pd.Series, ma: pd.Series) -> int:
    """Consecutive weeks the close has held above the moving average."""
    c = 0
    for px, m in zip(reversed(close.tolist()), reversed(ma.tolist())):
        if pd.isna(m) or pd.isna(px) or px < m:
            break
        c += 1
    return c


def overhead_supply(df: pd.DataFrame, price: float, lookback: int = 104) -> dict:
    """Share of recent traded volume that changed hands ABOVE the current price."""
    d = df.tail(lookback)
    vol = d["volume"].fillna(0)
    if vol.sum() <= 0:
        return {"overhead_supply_pct": None, "overhead_supply_verdict": "Not disclosed"}
    typical = d[["high", "low", "adjclose"]].mean(axis=1)
    above = vol[typical > price].sum()
    pct = float(above / vol.sum() * 100.0)
    verdict = ("Heavy" if pct > 55 else "Moderate" if pct > 25 else "Light")
    return {"overhead_supply_pct": round(pct, 1), "overhead_supply_verdict": verdict}


def analyse(df: pd.DataFrame, bench: pd.DataFrame | None,
            sector: pd.DataFrame | None = None) -> dict:
    """Compute the full technical record and assign exactly one Weinstein stage."""
    out: dict = {}
    n = len(df)
    out["weeks_history"] = n
    px = float(df["adjclose"].iloc[-1])
    out["price_inr"] = round(px, 2)
    out["technical_data_date"] = df["date"].iloc[-1].date().isoformat()

    if n < MIN_WEEKS:
        out["technical_stage"] = "Indeterminate-insufficient adjusted history"
        out["stage_rationale"] = f"only {n} adjusted weekly bars available (<{MIN_WEEKS})"
        return out

    ma30 = df["adjclose"].rolling(30).mean()
    ma40 = df["adjclose"].rolling(40).mean()
    out["ma30w_inr"] = round(float(ma30.iloc[-1]), 2) if not pd.isna(ma30.iloc[-1]) else None
    out["ma40w_inr"] = round(float(ma40.iloc[-1]), 2) if not pd.isna(ma40.iloc[-1]) else None
    s30 = _slope_pct(ma30, 13)
    s40 = _slope_pct(ma40, 13)
    out["ma30w_slope_pct"] = round(s30, 2) if s30 is not None else None
    out["ma40w_slope_pct"] = round(s40, 2) if s40 is not None else None
    out["distance_from_ma30w_pct"] = (
        round(_pct(px, out["ma30w_inr"]), 2) if out["ma30w_inr"] else None)

    hi52 = float(df["high"].tail(52).max())
    lo52 = float(df["low"].tail(52).min())
    out["high_52w"] = round(hi52, 2)
    out["low_52w"] = round(lo52, 2)
    out["distance_from_52w_high_pct"] = round(_pct(px, hi52), 2)

    for lb, tag in ((13, "3m"), (26, "6m"), (52, "12m")):
        if n > lb:
            out[f"return_{tag}_pct"] = round(_pct(px, float(df["adjclose"].iloc[-(lb + 1)])), 2)

    # ---- relative strength -------------------------------------------------
    def rs_vs(b: pd.DataFrame | None, label: str):
        if b is None or b.empty:
            out[f"rs_{label}_13w_pct"] = None
            out[f"rs_{label}_52w_pct"] = None
            return
        m = pd.merge(df[["date", "adjclose"]], b[["date", "adjclose"]],
                     on="date", how="inner", suffixes=("", "_b"))
        if len(m) < 30:
            out[f"rs_{label}_13w_pct"] = None
            out[f"rs_{label}_52w_pct"] = None
            return
        ratio = m["adjclose"] / m["adjclose_b"]
        for lb, key in ((13, "13w"), (52, "52w")):
            if len(ratio) > lb:
                out[f"rs_{label}_{key}_pct"] = round(
                    (ratio.iloc[-1] / ratio.iloc[-(lb + 1)] - 1) * 100, 2)
            else:
                out[f"rs_{label}_{key}_pct"] = None
        out[f"rs_{label}_ratio_ma13_rising"] = bool(
            ratio.rolling(13).mean().iloc[-1] > ratio.rolling(13).mean().iloc[-5]
        ) if len(ratio) > 18 else None

    rs_vs(bench, "bm")
    rs_vs(sector, "sector")

    rs13 = out.get("rs_bm_13w_pct")
    rs52 = out.get("rs_bm_52w_pct")
    out["rs_trend"] = ("Improving" if (rs13 or 0) > 2 else
                       "Deteriorating" if (rs13 or 0) < -2 else "Flat") \
        if rs13 is not None else "Not disclosed"

    # ---- base / pivot ------------------------------------------------------
    base = find_base(df)
    out.update({k: v for k, v in base.items() if k != "base_start_idx"})

    # ---- volume ------------------------------------------------------------
    vol = df["volume"].fillna(0)
    v4 = float(vol.tail(4).mean())
    v20 = float(vol.tail(20).mean())
    v50 = float(vol.tail(50).mean())
    out["vol_4w_avg"] = round(v4, 0)
    out["vol_20w_avg"] = round(v20, 0)
    out["volume_confirmation_metric"] = "4w avg vol / 20w avg vol"
    out["volume_confirmation_value"] = round(v4 / v20, 2) if v20 else None
    out["vol_4w_vs_50w"] = round(v4 / v50, 2) if v50 else None

    out.update(overhead_supply(df, px))

    wk_above = weeks_above_ma(df["adjclose"], ma30)
    out["weeks_above_ma30"] = wk_above

    # ---- stage assignment --------------------------------------------------
    above30 = px > (out["ma30w_inr"] or px)
    above40 = px > (out["ma40w_inr"] or px)
    dist30 = out["distance_from_ma30w_pct"] or 0.0
    s30v = s30 if s30 is not None else 0.0
    ret52 = out.get("return_12m_pct")
    pivot = out.get("pivot_level")
    base_dur = out.get("base_duration_weeks") or 0

    # prior advance measured up to 13 weeks ago - distinguishes Stage 3 from Stage 1
    prior_adv = None
    if n > 65:
        prior_adv = _pct(float(df["adjclose"].iloc[-14]), float(df["adjclose"].iloc[-66]))

    if not above30 and s30v < -FLAT_SLOPE:
        stage = "Stage 4 decline"
        why = f"price {dist30:.1f}% below a falling 30w MA (slope {s30v:.1f}%/13w)"
    elif s30v > FLAT_SLOPE and above30 and above40:
        if dist30 > EXTENDED_ABOVE_MA30 or wk_above > LONG_STAGE2_WEEKS:
            stage = "Mature/extended Stage 2"
            why = (f"advancing but extended: {dist30:.1f}% above a rising 30w MA, "
                   f"{wk_above}w above it")
        else:
            stage = "Early Stage 2"
            why = (f"price above rising 30w/40w MA (slope {s30v:.1f}%/13w), "
                   f"{dist30:.1f}% above MA30, {wk_above}w above")
    elif abs(s30v) <= FLAT_SLOPE:
        # flat MA - either basing (Stage 1) or topping (Stage 3)
        topping = (prior_adv is not None and prior_adv > 30 and
                   (out["distance_from_52w_high_pct"] or 0) > -22)
        if topping and above30:
            stage = "Stage 3 distribution"
            why = (f"30w MA flattened ({s30v:.1f}%/13w) after a {prior_adv:.0f}% advance; "
                   f"price stalling {out['distance_from_52w_high_pct']:.1f}% off the 52w high")
        elif above30 and pivot and px >= pivot * 0.995 and s30v > 0:
            stage = "Stage 1-to-Stage 2 transition"
            why = (f"price reclaiming the {base_dur}w base pivot {pivot} with the "
                   f"30w MA turning up ({s30v:.1f}%/13w)")
        elif base_dur >= 20:
            stage = "Mature Stage 1 base"
            why = (f"{base_dur}w base, depth {out.get('base_depth_pct')}%, "
                   f"30w MA flat ({s30v:.1f}%/13w)")
        else:
            stage = "Early Stage 1"
            why = (f"30w MA only just flattening ({s30v:.1f}%/13w) after decline; "
                   f"base still short ({base_dur}w)")
    elif s30v > FLAT_SLOPE and not above30:
        stage = "Stage 3 distribution"
        why = (f"price has broken below a still-rising 30w MA "
               f"({dist30:.1f}%) - momentum rolling over")
    elif s30v < -FLAT_SLOPE and above30:
        stage = "Early Stage 1"
        why = (f"price recovered above a still-falling 30w MA "
               f"(slope {s30v:.1f}%/13w) - early basing attempt")
    else:
        stage = "Early Stage 1"
        why = f"mixed signals: slope {s30v:.1f}%/13w, price {dist30:.1f}% vs MA30"

    out["technical_stage"] = stage
    out["stage_rationale"] = why

    # entry extension + levels
    if dist30 > EXTENDED_ABOVE_MA30:
        out["entry_extension_verdict"] = "Extended - do not chase"
    elif dist30 > 12:
        out["entry_extension_verdict"] = "Moderately extended"
    elif dist30 >= -6:
        out["entry_extension_verdict"] = "Near valid entry zone"
    else:
        out["entry_extension_verdict"] = "Below MA30 - no valid entry yet"

    out["support_level_inr"] = out.get("base_low") or (
        round(min(out["ma30w_inr"] or px, lo52 * 1.02), 2))
    inval = out.get("base_low")
    if inval is None and out["ma30w_inr"]:
        inval = round(out["ma30w_inr"] * 0.92, 2)
    out["invalidation_level_inr"] = inval
    out["technical_trigger"] = (
        f"weekly close above {pivot} on >1.5x 20w avg volume" if pivot else "Not disclosed")
    return out
