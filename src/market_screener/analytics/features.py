"""
Technical feature computation over the whole universe.

Division of labour, per the two-engine contract:

* **DuckDB** scans weekly_bar once, resolves the winning source per week, and
  hands back a single tidy frame. That is the part that benefits from a columnar
  engine.
* **Python** computes every derived number, using `domain/weinstein.py`, which is
  a verbatim port of the frozen oracle. No moving average, slope or relative
  strength is ever recomputed in SQL.

Loading all bars once and grouping in memory is far cheaper than a query per
security - the legacy pipeline's per-symbol file reads were the slow part.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable

import pandas as pd
import pyarrow as pa

from ..config import Settings
from ..domain import weinstein
from .duck import analytics_session, load_sql

log = logging.getLogger(__name__)

BROAD_BENCHMARK = "NIFTY_500"
FALLBACK_BENCHMARK = "NIFTY_50"

# NSE industry -> sector index used for the sector relative-strength leg.
SECTOR_BENCHMARK = {
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


def _arrow(con, sql: str, params: dict) -> pa.Table:
    tbl = con.execute(sql, params).arrow()
    return tbl.read_all() if isinstance(tbl, pa.RecordBatchReader) else tbl


class BasisIncoherent(RuntimeError):
    """The benchmark is not on the same return basis as the securities."""


def load_all_weekly(settings: Settings, as_of: date) -> pd.DataFrame:
    """
    Every complete weekly bar up to as_of, on ONE return basis.

    The basis is fixed for the whole run. Yahoo's adjclose is total return and
    the bhavcopy series is price return, so a series that switches between them
    steps by the cumulative dividend yield - and relative strength divides a
    stock series by a benchmark series, which is meaningless unless both sides
    measure the same thing.
    """
    with analytics_session(settings) as con:
        tbl = _arrow(con, load_sql("weekly_series.sql"),
                     {"as_of": as_of, "adj_basis": settings.price_basis})
    df = tbl.to_pandas()

    if not df.empty:
        bases = set(df["adj_basis"].unique())
        if bases != {settings.price_basis}:
            raise BasisIncoherent(
                f"expected only {settings.price_basis!r}, got {sorted(bases)}")
        mixed = (df.groupby("security_id")["source"].nunique() > 1).sum()
        if mixed:
            raise BasisIncoherent(
                f"{mixed} securities still carry more than one source; "
                f"weekly_bar_resolved must elect a single source per security")

    log.info("loaded %d weekly bars for %d securities on basis %s",
             len(df), df["security_id"].nunique() if len(df) else 0,
             settings.price_basis)
    return df


def load_security_meta(settings: Settings) -> pd.DataFrame:
    with analytics_session(settings) as con:
        tbl = _arrow(con, """
            SELECT security_id, symbol, company_name, nse_industry, security_type,
                   is_active, series
            FROM   src_security
        """, {})
    return tbl.to_pandas()


def compute_universe(settings: Settings, as_of: date,
                     security_ids: Iterable[int] | None = None) -> list[dict]:
    """
    Run the Weinstein analysis for every equity in the universe.

    Returns one record per security: the full technical bundle plus the assigned
    stage, ready to be written to technical_feature and read by the screen.
    """
    bars = load_all_weekly(settings, as_of)
    meta = load_security_meta(settings)
    if bars.empty:
        return []

    by_id = {int(r.security_id): r for r in meta.itertuples(index=False)}
    sym_to_id = {r.symbol: int(r.security_id) for r in meta.itertuples(index=False)}

    grouped = {int(sid): g for sid, g in bars.groupby("security_id", sort=False)}

    def bench_frame(name: str) -> pd.DataFrame | None:
        sid = sym_to_id.get(name)
        if sid is None or sid not in grouped:
            return None
        return weinstein.bars_from_frame(grouped[sid])

    # `or` on a DataFrame raises; test for None explicitly.
    broad = bench_frame(BROAD_BENCHMARK)
    if broad is None:
        broad = bench_frame(FALLBACK_BENCHMARK)
    if broad is None:
        raise BasisIncoherent(
            f"no benchmark series on basis {settings.price_basis!r}. Relative "
            f"strength divides a stock by a benchmark, so both must be on the "
            f"same basis - a price-return run needs price-return indices, which "
            f"bhavcopy does not carry.")
    sector_frames = {name: bench_frame(name)
                     for name in set(SECTOR_BENCHMARK.values())}

    wanted = (set(int(s) for s in security_ids) if security_ids is not None
              else {int(sid) for sid, m in by_id.items()
                    if m.security_type == "equity" and m.is_active})

    out: list[dict] = []
    for sid in sorted(wanted):
        g = grouped.get(sid)
        if g is None:
            continue
        m = by_id.get(sid)
        frame = weinstein.bars_from_frame(g)
        if frame is None:
            continue
        industry = getattr(m, "nse_industry", None) if m else None
        sector = sector_frames.get(SECTOR_BENCHMARK.get(industry or "", ""))

        rec = weinstein.analyse(frame, broad, sector)
        rec["security_id"] = sid
        rec["symbol"] = getattr(m, "symbol", None) if m else None
        rec["benchmark_symbol"] = BROAD_BENCHMARK
        rec["sector_benchmark"] = SECTOR_BENCHMARK.get(industry or "")
        rec["bar_source"] = (g["source"].iloc[-1] if "source" in g else None)
        rec["adj_basis"] = (g["adj_basis"].iloc[-1] if "adj_basis" in g else None)
        rec["liquidity_inr_cr"], rec["liquidity_period"] = liquidity_from_bars(frame)
        out.append(rec)
    log.info("computed technicals for %d securities", len(out))
    return out


def liquidity_from_bars(df: pd.DataFrame) -> tuple[float | None, str]:
    """
    13-week median DAILY traded value in INR crore, derived from weekly bars.

    Kept identical to the legacy definition so the eligibility gate does not
    shift under the port. Now that bhavcopy turnover is actually being ingested,
    a true daily median becomes possible - that is a deliberate change to make
    later, with the difference measured, not a silent one.
    """
    import numpy as np
    if df is None or len(df) < 13:
        return None, "insufficient bars"
    d = df.tail(13)
    typical = d[["high", "low", "adjclose"]].mean(axis=1)
    weekly_val = (typical * d["volume"]).dropna()
    if weekly_val.empty:
        return None, "no volume"
    return round(float(np.median(weekly_val)) / 5.0 / 1e7, 3), \
        "13-week median of weekly traded value / 5"
