"""
Reporting-period labels.

screener column headers are not all "Mar 2026". Two real variants appear in the
2,086-company cache and both were being dropped silently by a Mar/Jun/Sep/Dec
regex:

* **"Mar 2023 15m"** - a transition period. A company that moves its year end
  files one long accounting period; ACC's is 15 months. Dropping the column
  loses a whole year of the P&L, and treating it as an ordinary 12-month year
  would overstate growth.
* **"Jul 2026"** - shareholding disclosed at a month end that is not a calendar
  quarter.

The duration is carried in `period_type` (`annual_15m`) rather than a new column,
so the label round-trips exactly and a non-standard period can be excluded from
CAGR arithmetic instead of quietly distorting it.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

PERIOD_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{4})(?:\s+(\d+)\s*m)?$")

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_period_label(label: str, base_period_type: str) -> tuple[str, date] | None:
    """
    'Mar 2026'      -> ('annual', 2026-03-31)
    'Mar 2023 15m'  -> ('annual_15m', 2023-03-31)
    'Jul 2026'      -> ('quarter', 2026-07-31)
    'TTM'           -> None  (the caller resolves TTM against the latest quarter)
    """
    s = (label or "").strip()
    m = PERIOD_RE.match(s)
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    year = int(m.group(2))
    last_day = calendar.monthrange(year, mon)[1]
    months = m.group(3)
    period_type = f"{base_period_type}_{int(months)}m" if months else base_period_type
    return period_type, date(year, mon, last_day)


def format_period_label(report_date: date, period_type: str) -> str:
    """Inverse of parse_period_label; must reproduce the source header exactly."""
    if period_type == "ttm":
        return "TTM"
    base = f"{calendar.month_abbr[report_date.month]} {report_date.year}"
    m = re.match(r"^[a-z]+_(\d+)m$", period_type)
    return f"{base} {m.group(1)}m" if m else base


def is_standard_length(period_type: str) -> bool:
    """False for a transition period, which must not be used in CAGR arithmetic."""
    return "_" not in period_type or period_type.startswith("ttm")


def base_type(period_type: str) -> str:
    """'annual_15m' -> 'annual'."""
    return period_type.split("_", 1)[0]
