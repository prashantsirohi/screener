"""
NSE daily index closes.

The unlock for the price-return cutover. Relative strength divides a stock series
by a benchmark series, so both must be on the same return basis - and until now
the only benchmark series available came from Yahoo, which is total return. That
pinned the whole technical layer to the Yahoo basis.

`ind_close_all_<DDMMYYYY>.csv` gives exchange-published OHLC for ~163 indices,
which is price return by construction. With these the bhavcopy basis has
benchmarks of its own.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

from ..http.client import HttpClient, nse_client
from ..http.errors import PermanentHttpError, TemporaryHttpError

log = logging.getLogger(__name__)

URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"
REFERER = "https://www.nseindia.com/all-reports"

# NSE's display name -> the benchmark symbol already used in the store.
# Keyed on a normalised form so casing and spacing changes do not break it.
INDEX_SYMBOLS = {
    "nifty 50": "NIFTY_50",
    "nifty 500": "NIFTY_500",
    "nifty bank": "NIFTY_BANK",
    "nifty it": "NIFTY_IT",
    "nifty pharma": "NIFTY_PHARMA",
    "nifty auto": "NIFTY_AUTO",
    "nifty fmcg": "NIFTY_FMCG",
    "nifty metal": "NIFTY_METAL",
    "nifty energy": "NIFTY_ENERGY",
    "nifty infrastructure": "NIFTY_INFRA",
    "nifty realty": "NIFTY_REALTY",
    "nifty psu bank": "NIFTY_PSUBANK",
}

REQUIRED = {"Index Name", "Index Date", "Closing Index Value"}


class IndexCloseUnavailable(Exception):
    """No index close file published for this date."""


@dataclass
class IndexDay:
    trade_date: date
    frame: pd.DataFrame     # symbol, open, high, low, close, volume, turnover_inr
    url: str


def _norm(name: str) -> str:
    return " ".join(str(name or "").strip().lower().split())


def _num(s):
    return pd.to_numeric(
        pd.Series(s).astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce")


class IndexCloseCollector:
    def __init__(self, client: HttpClient | None = None):
        self.http = client or nse_client(referer=REFERER)

    def fetch(self, d: date) -> IndexDay:
        url = URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
        try:
            resp = self.http.fetch_with_retries(
                lambda: self.http.get(url, headers={"Referer": REFERER}),
                description=f"index close {d}")
        except (PermanentHttpError, TemporaryHttpError) as exc:
            raise IndexCloseUnavailable(f"{d}: {exc}") from exc

        df = pd.read_csv(io.BytesIO(resp.content))
        df.columns = [c.strip() for c in df.columns]
        missing = REQUIRED - set(df.columns)
        if missing:
            raise ValueError(f"index close file missing columns: {sorted(missing)}")

        df["_key"] = df["Index Name"].map(_norm)
        keep = df[df["_key"].isin(INDEX_SYMBOLS)].copy()
        if keep.empty:
            raise IndexCloseUnavailable(f"{d}: none of the tracked indices present")

        out = pd.DataFrame({
            "symbol": keep["_key"].map(INDEX_SYMBOLS),
            "open": _num(keep.get("Open Index Value")).values,
            "high": _num(keep.get("High Index Value")).values,
            "low": _num(keep.get("Low Index Value")).values,
            "close": _num(keep["Closing Index Value"]).values,
            "volume": _num(keep.get("Volume")).values if "Volume" in keep else None,
            # Index turnover is reported in INR crore; price_daily stores rupees.
            "turnover_inr": (_num(keep.get("Turnover (Rs. Cr.)")).values * 1e7
                             if "Turnover (Rs. Cr.)" in keep else None),
        })
        out = out[out["close"].notna() & (out["close"] > 0)].reset_index(drop=True)
        if out.empty:
            raise IndexCloseUnavailable(f"{d}: no usable index closes")
        return IndexDay(trade_date=d, frame=out, url=url)
