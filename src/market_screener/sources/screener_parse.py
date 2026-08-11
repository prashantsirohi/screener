"""
screener.in company-page parsing and blank-shell detection.

Parsing is lifted from the legacy `02_fetch_fundamentals.py` so the payload
shape is byte-compatible with the 2,086 JSON files already on disk. That
compatibility is what lets the existing cache be imported instead of re-scraped.

The blank-shell detector is the important addition. Under sustained scraping
screener returns HTTP 200 with the full page skeleton and every numeric span
emptied - 307 of 2,086 companies came back this way. Nothing at the transport
layer distinguishes it from a good page, so detection has to be structural.
"""

from __future__ import annotations

import re
from typing import Any

from lxml import html as lhtml

NUM_RE = re.compile(r"-?[\d,]+\.?\d*")
FY_RE = re.compile(r"^(Mar|Jun|Sep|Dec)\s+\d{4}$")

SECTIONS = ("profit-loss", "balance-sheet", "cash-flow", "ratios",
            "quarters", "shareholding")


def to_num(txt: str | None) -> float | None:
    """'1,39,880' -> 139880.0; '48.8%' -> 48.8; '' -> None. Indian grouping."""
    if txt is None:
        return None
    t = (txt.strip().replace(",", "").replace("₹", "")
         .replace("%", "").replace("Cr.", "").strip())
    if t in ("", "-", "--", "NA", "N/A"):
        return None
    neg = t.startswith("-")
    m = NUM_RE.search(t[1:] if neg else t)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


def parse_top_ratios(doc) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for li in doc.cssselect("#top-ratios li"):
        name_el = li.cssselect("span.name")
        val_el = li.cssselect("span.value")
        if not name_el or not val_el:
            continue
        name = " ".join(name_el[0].text_content().split())
        nums = [to_num(n.text_content()) for n in val_el[0].cssselect("span.number")]
        raw = " ".join(val_el[0].text_content().split())
        if name.lower().startswith("high"):
            out["high_52w"] = nums[0] if len(nums) > 0 else None
            out["low_52w"] = nums[1] if len(nums) > 1 else None
        else:
            out[name] = nums[0] if nums else to_num(raw)
    return out


def parse_section_table(doc, section_id: str) -> dict[str, dict[str, float | None]]:
    secs = doc.cssselect(f"section#{section_id} table.data-table")
    if not secs:
        return {}
    tbl = secs[0]
    heads = [" ".join(th.text_content().split()) for th in tbl.cssselect("thead th")]
    periods = heads[1:] if heads else []
    out: dict[str, dict[str, float | None]] = {}
    for tr in tbl.cssselect("tbody tr"):
        tds = tr.cssselect("td")
        if not tds:
            continue
        label = " ".join(tds[0].text_content().split()).replace("\xa0", " ").rstrip("+").strip()
        if not label:
            continue
        out[label] = {p: to_num(td.text_content()) for p, td in zip(periods, tds[1:])}
    return out


def parse_growth_tables(doc) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for tbl in doc.cssselect("section#profit-loss table.ranges-table"):
        head = tbl.cssselect("th")
        if not head:
            continue
        key = " ".join(head[0].text_content().split()).rstrip(":")
        rows: dict[str, float | None] = {}
        for tr in tbl.cssselect("tr")[1:]:
            tds = tr.cssselect("td")
            if len(tds) >= 2:
                rows[" ".join(tds[0].text_content().split()).rstrip(":")] = \
                    to_num(tds[1].text_content())
        out[key] = rows
    return out


def parse_company_page(html_text: str, symbol: str, url: str,
                       basis: str) -> dict[str, Any]:
    doc = lhtml.fromstring(html_text)
    rec: dict[str, Any] = {
        "symbol": symbol,
        "source_url": url,
        "basis": basis,
        "name": (" ".join(doc.cssselect("h1")[0].text_content().split())
                 if doc.cssselect("h1") else None),
        "top_ratios": parse_top_ratios(doc),
        "growth": parse_growth_tables(doc),
    }
    for sec in SECTIONS:
        rec[sec.replace("-", "_")] = parse_section_table(doc, sec)
    return rec


# --------------------------------------------------------------------------
# Blank-shell detection
# --------------------------------------------------------------------------

def is_blank_payload(rec: dict[str, Any]) -> tuple[bool, str | None]:
    """
    Decide whether a parsed payload is a data-free shell.

    Returns (is_blank, reason). The distinction that matters:

    * `no_tables` - the page had no statement sections at all. Usually a symbol
      screener does not cover, and re-fetching will not help.
    * `numeric_spans_empty` - the sections and row labels rendered but every
      value is null. This is the throttle signature and IS worth retrying.

    A genuinely tiny company still reports *some* number, so "every value across
    every statement is null" is a safe test - it has no false positives in the
    2,086-company cache.
    """
    if rec.get("error"):
        return True, "fetch_error"

    tables = {k: rec.get(k) or {} for k in
              ("profit_loss", "balance_sheet", "cash_flow", "ratios", "quarters")}
    has_labels = any(bool(t) for t in tables.values())
    top = rec.get("top_ratios") or {}
    has_top_labels = bool(top)

    if not has_labels and not has_top_labels:
        return True, "no_tables"

    top_has_value = any(v is not None for v in top.values())
    table_has_value = any(
        v is not None
        for t in tables.values()
        for row in t.values()
        for v in row.values()
    )
    if not top_has_value and not table_has_value:
        return True, "numeric_spans_empty"
    return False, None


def blank_reason_is_retryable(reason: str | None) -> bool:
    """`no_tables` means screener has no page for this symbol; do not loop on it."""
    return reason in ("numeric_spans_empty", "fetch_error")
