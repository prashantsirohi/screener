"""
NSE daily bhavcopy collector.

NSE replaced the legacy `cm<DDMMMYYYY>bhav.csv.zip` layout with UDiFF during
2024. The exact cutover is not documented in a way worth trusting, so both
readers are implemented and the fetcher probes: UDiFF first, legacy second, and
the result is cached per year so the wrong one is not retried every day.

A weekday with no bhavcopy under either layout is a trading holiday. That is how
the trading calendar gets built - from what the exchange actually published,
rather than from a hardcoded holiday list that goes stale every January.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Iterator

import pandas as pd

from ..http.client import HttpClient, nse_client
from ..http.errors import PermanentHttpError, TemporaryHttpError

log = logging.getLogger(__name__)

UDIFF_URL = ("https://nsearchives.nseindia.com/content/cm/"
             "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip")
LEGACY_URL = ("https://nsearchives.nseindia.com/content/historical/EQUITIES/"
              "{yyyy}/{MMM}/cm{ddMMMyyyy}bhav.csv.zip")

REFERER = "https://www.nseindia.com/all-reports"

# Normalised column set every reader must produce.
CANONICAL = ("symbol", "series", "open", "high", "low", "close", "prev_close",
             "volume", "turnover_inr", "trade_count", "isin")

UDIFF_REQUIRED = {"TCKRSYMB", "SCTYSRS", "CLSPRIC", "TTLTRADGVOL", "TTLTRFVAL"}
LEGACY_REQUIRED = {"SYMBOL", "SERIES", "CLOSE", "TOTTRDQTY", "TOTTRDVAL"}


class BhavcopyUnavailable(Exception):
    """No bhavcopy published for this date under either layout."""


@dataclass
class BhavcopyDay:
    trade_date: date
    layout: str            # "udiff" | "legacy"
    frame: pd.DataFrame    # canonical columns, series-EQ only
    url: str


def _unzip_csv(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("zip contains no csv")
        with z.open(names[0]) as fh:
            return pd.read_csv(fh, low_memory=False)


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def parse_udiff(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip().upper() for c in df.columns]
    missing = UDIFF_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"UDiFF bhavcopy missing columns: {sorted(missing)}")

    eq = df[df["SCTYSRS"].astype(str).str.strip() == "EQ"].copy()
    out = pd.DataFrame({
        "symbol": eq["TCKRSYMB"].astype(str).str.strip(),
        "series": "EQ",
        "open": _num(eq.get("OPNPRIC")),
        "high": _num(eq.get("HGHPRIC")),
        "low": _num(eq.get("LWPRIC")),
        "close": _num(eq["CLSPRIC"]),
        "prev_close": _num(eq.get("PRVSCLSGPRIC")),
        "volume": _num(eq["TTLTRADGVOL"]),
        "turnover_inr": _num(eq["TTLTRFVAL"]),
        "trade_count": _num(eq.get("TTLNBOFTXSEXCTD")),
        "isin": eq.get("ISIN").astype(str).str.strip() if "ISIN" in eq else None,
    })
    return out


def parse_legacy(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip().upper() for c in df.columns]
    missing = LEGACY_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"legacy bhavcopy missing columns: {sorted(missing)}")

    eq = df[df["SERIES"].astype(str).str.strip() == "EQ"].copy()
    out = pd.DataFrame({
        "symbol": eq["SYMBOL"].astype(str).str.strip(),
        "series": "EQ",
        "open": _num(eq.get("OPEN")),
        "high": _num(eq.get("HIGH")),
        "low": _num(eq.get("LOW")),
        "close": _num(eq["CLOSE"]),
        "prev_close": _num(eq.get("PREVCLOSE")),
        "volume": _num(eq["TOTTRDQTY"]),
        "turnover_inr": _num(eq["TOTTRDVAL"]),
        "trade_count": _num(eq.get("TOTALTRADES")),
        "isin": eq.get("ISIN").astype(str).str.strip() if "ISIN" in eq else None,
    })
    return out


class BhavcopyCollector:
    def __init__(self, client: HttpClient | None = None):
        self.http = client or nse_client(referer=REFERER)
        # Remembers which layout worked per year so the dead one is not probed
        # for every date in a long backfill.
        self._layout_by_year: dict[int, str] = {}

    def _urls_for(self, d: date) -> Iterator[tuple[str, str]]:
        udiff = ("udiff", UDIFF_URL.format(yyyymmdd=d.strftime("%Y%m%d")))
        legacy = ("legacy", LEGACY_URL.format(
            yyyy=d.strftime("%Y"), MMM=d.strftime("%b").upper(),
            ddMMMyyyy=d.strftime("%d%b%Y").upper()))
        known = self._layout_by_year.get(d.year)
        if known == "legacy":
            yield legacy
            yield udiff
        else:
            yield udiff
            yield legacy

    def fetch(self, d: date) -> BhavcopyDay:
        """Fetch one day. Raises BhavcopyUnavailable when nothing was published."""
        errors: list[str] = []
        for layout, url in self._urls_for(d):
            try:
                resp = self.http.fetch_with_retries(
                    lambda u=url: self.http.get(u, headers={"Referer": REFERER}),
                    description=f"bhavcopy {d} {layout}")
            except PermanentHttpError as exc:
                errors.append(f"{layout}: {exc}")
                continue
            except TemporaryHttpError as exc:
                errors.append(f"{layout}: {exc}")
                continue

            try:
                raw = _unzip_csv(resp.content)
                frame = parse_udiff(raw) if layout == "udiff" else parse_legacy(raw)
            except (zipfile.BadZipFile, ValueError) as exc:
                errors.append(f"{layout}: unreadable ({exc})")
                continue

            if frame.empty:
                errors.append(f"{layout}: no series-EQ rows")
                continue

            self._layout_by_year[d.year] = layout
            log.debug("bhavcopy %s via %s: %d rows", d, layout, len(frame))
            return BhavcopyDay(trade_date=d, layout=layout, frame=frame, url=url)

        raise BhavcopyUnavailable(f"{d}: {'; '.join(errors)}")
