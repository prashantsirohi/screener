"""
NSE quote-equity collector - authoritative share count.

`securityInfo.issuedSize` is the number of listed shares straight from the
exchange. Combined with the bhavcopy close it yields a market cap that owes
nothing to the aggregator, which matters because 307 companies had no market cap
at all when screener.in served blank pages - and the previous fallback (equity
capital / face value) could not help, since equity capital came from the same
blank page.

Also returns ISIN and NSE's own industry label, which fills sector gaps for the
~1,300 symbols outside the index constituent files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from ..http.client import HttpClient, nse_client
from ..http.errors import PermanentHttpError

log = logging.getLogger(__name__)

QUOTE_URL = "https://www.nseindia.com/api/quote-equity"
REFERER = "https://www.nseindia.com/get-quotes/equity"


@dataclass
class QuoteInfo:
    symbol: str
    issued_size: int | None
    isin: str | None
    industry: str | None
    last_price: float | None
    face_value: float | None
    listing_date: date | None
    raw_keys: tuple[str, ...] = ()


def _f(v) -> float | None:
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


class QuoteCollector:
    def __init__(self, client: HttpClient | None = None):
        self.http = client or nse_client(referer=REFERER)

    def fetch(self, symbol: str) -> QuoteInfo:
        def _go():
            return self.http.get_json(
                QUOTE_URL, params={"symbol": symbol},
                headers={"Referer": f"{REFERER}?symbol={symbol}"})

        payload = self.http.fetch_with_retries(_go, description=f"quote {symbol}")
        if not isinstance(payload, dict) or not payload:
            raise PermanentHttpError(f"empty quote payload for {symbol}")

        info = payload.get("info") or {}
        sec = payload.get("securityInfo") or {}
        meta = payload.get("metadata") or {}
        price = payload.get("priceInfo") or {}

        listing = None
        raw_listing = meta.get("listingDate") or info.get("listingDate")
        if raw_listing:
            for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
                try:
                    from datetime import datetime
                    listing = datetime.strptime(str(raw_listing).strip(), fmt).date()
                    break
                except ValueError:
                    continue

        return QuoteInfo(
            symbol=symbol,
            issued_size=_i(sec.get("issuedSize")),
            isin=(info.get("isin") or meta.get("isin") or None),
            industry=(meta.get("industry") or info.get("industry") or None),
            last_price=_f(price.get("lastPrice")),
            face_value=_f(sec.get("faceValue")),
            listing_date=listing,
            raw_keys=tuple(payload.keys()),
        )
