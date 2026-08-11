"""
NSE corporate actions API - the authoritative source for splits and bonuses.

Price-based inference gets most of the way but cannot resolve a shallow bonus
from an ordinary bad day, and cannot see an action at all if both price series
already agree. This feed states the action outright.

The work is in parsing NSE's free-text `subject` into an adjustment factor:

    "Face Value Split (Sub-Division) - From Rs 10/- To Rs 1/-"  -> 0.1
    "Bonus 1:1"                                                 -> 0.5
    "Bonus 3:5"                                                 -> 0.625

A bonus of a:b means a new shares for every b held, so the share count goes from
b to a+b and the price factor is b/(a+b). Splits are the face-value ratio.
Dividends and rights are ignored: the adjusted series is a PRICE-return basis,
and rights pricing needs a subscription price this feed does not carry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator

from ..http.client import HttpClient, nse_client
from ..http.errors import HttpError

log = logging.getLogger(__name__)

URL = "https://www.nseindia.com/api/corporates-corporateActions"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-actions"

# NSE writes the singular "Re 1/-" for one rupee and "Rs 10/-" otherwise, so the
# currency token has to accept both spellings.
_RUPEE = r"(?:rs|re)\.?\s*"

SPLIT_RE = re.compile(
    rf"(?:face\s*value\s*)?split.*?from\s*{_RUPEE}?([\d.]+).*?to\s*{_RUPEE}?([\d.]+)",
    re.I | re.S)
BONUS_RE = re.compile(r"bonus\s*(?:issue\s*)?[^0-9]{0,12}(\d+)\s*[:/]\s*(\d+)", re.I)
CONSOLIDATION_RE = re.compile(
    rf"consolidat.*?from\s*{_RUPEE}?([\d.]+).*?to\s*{_RUPEE}?([\d.]+)", re.I | re.S)


@dataclass
class CorporateAction:
    symbol: str
    ex_date: date
    action_type: str          # split | bonus | consolidation
    factor: float             # multiply pre-ex prices by this
    ratio_from: float | None
    ratio_to: float | None
    subject: str


def parse_subject(subject: str) -> tuple[str, float, float, float] | None:
    """
    -> (action_type, factor, ratio_from, ratio_to), or None when not a
    price-affecting split/bonus.
    """
    s = " ".join((subject or "").split())
    if not s:
        return None
    low = s.lower()

    # Rights need a subscription price this feed does not provide.
    if "rights" in low:
        return None

    # A bonus issued in a DIFFERENT instrument class does not change the equity
    # share count and must not adjust the equity price. TVS Motor's
    # "Scheme Of Arrangement - Bonus Ncrps 4:1" was being read as a 4:1 equity
    # bonus and would have rescaled its whole history by 0.2.
    if any(tok in low for tok in ("ncrps", "ncd", "preference", "pref share",
                                  "debenture", "warrant", "ncps", "rps")):
        return None

    m = SPLIT_RE.search(s)
    if m:
        try:
            frm, to = float(m.group(1)), float(m.group(2))
        except ValueError:
            return None
        if frm > 0 and to > 0 and to != frm:
            factor = to / frm                     # FV 10 -> 1 halves price tenfold
            kind = "split" if to < frm else "consolidation"
            return kind, factor, to, frm

    m = CONSOLIDATION_RE.search(s)
    if m:
        try:
            frm, to = float(m.group(1)), float(m.group(2))
        except ValueError:
            return None
        if frm > 0 and to > 0 and to != frm:
            return "consolidation", to / frm, to, frm

    m = BONUS_RE.search(s)
    if m:
        try:
            a, b = float(m.group(1)), float(m.group(2))
        except ValueError:
            return None
        if a > 0 and b > 0:
            # a new for every b held: b shares become a+b
            return "bonus", b / (a + b), b, a + b

    return None


class CorporateActionCollector:
    def __init__(self, client: HttpClient | None = None):
        self.http = client or nse_client(referer=REFERER)

    def fetch_window(self, frm: date, to: date) -> list[dict]:
        def _go():
            return self.http.get_json(URL, params={
                "index": "equities",
                "from_date": frm.strftime("%d-%m-%Y"),
                "to_date": to.strftime("%d-%m-%Y"),
            }, headers={"Referer": REFERER})

        payload = self.http.fetch_with_retries(
            _go, description=f"corporate actions {frm}..{to}")
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("rows") or []
        return payload or []

    def collect(self, start: date, end: date,
                window_days: int = 60) -> Iterator[CorporateAction]:
        cur = start
        while cur <= end:
            stop = min(cur + timedelta(days=window_days), end)
            try:
                rows = self.fetch_window(cur, stop)
            except HttpError as exc:
                log.warning("corporate actions %s..%s failed: %s", cur, stop, exc)
                cur = stop + timedelta(days=1)
                continue

            log.info("corporate actions %s..%s: %d rows", cur, stop, len(rows))
            for r in rows:
                sym = str(r.get("symbol") or "").strip()
                subject = r.get("subject") or r.get("purpose") or ""
                raw_ex = (r.get("exDate") or r.get("ex_date") or "").strip()
                if not sym or not raw_ex:
                    continue
                ex = None
                for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
                    try:
                        ex = datetime.strptime(raw_ex, fmt).date()
                        break
                    except ValueError:
                        continue
                if ex is None:
                    continue
                parsed = parse_subject(subject)
                if not parsed:
                    continue
                kind, factor, rf, rt = parsed
                if not (0 < factor < 100) or factor == 1.0:
                    continue
                yield CorporateAction(symbol=sym, ex_date=ex, action_type=kind,
                                      factor=factor, ratio_from=rf, ratio_to=rt,
                                      subject=" ".join(subject.split())[:300])
            cur = stop + timedelta(days=1)
