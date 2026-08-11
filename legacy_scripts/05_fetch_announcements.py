"""
Phase 1 / Step 5 - NSE corporate announcements + corporate actions.

Financial statements cannot reveal a demerger, scheme of arrangement, open offer,
delisting or asset sale. Without this feed the "Event-driven / special situation"
archetype is structurally undetectable, so it is pulled directly from NSE - a
primary source.

Outputs:
    data/raw/nse_announcements.csv
    data/raw/nse_corporate_actions.csv
    data/raw/event_flags.csv     (per-symbol event classification)
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Keyword -> (event class, controlled secondary tag)
EVENT_PATTERNS = [
    (r"scheme of arrangement|demerger|de-merger|demerged|composite scheme",
     "Demerger / scheme of arrangement", "Demerger/SOTP unlocking"),
    # NB: "substantial acquisition of shares" alone is the SAST regulation title and
    # appears on routine 2%-threshold disclosures - it is NOT evidence of an open offer.
    (r"open offer|detailed public statement|letter of offer|"
     r"public announcement under regulation 3|change in control",
     "Open offer / control change", "Regulatory catalyst"),
    (r"\bdelisting\b|voluntary delisting", "Delisting", "Regulatory catalyst"),
    (r"buy-?back of equity|buyback of equity|buy back of equity",
     "Buyback", "Regulatory catalyst"),
    (r"capital reduction|reduction of (share )?capital", "Capital reduction",
     "Demerger/SOTP unlocking"),
    (r"slump sale|sale of (the )?(undertaking|business|division|subsidiary)|divestment|"
     r"stake sale|monetisation|monetization",
     "Asset / business sale", "Demerger/SOTP unlocking"),
    (r"initial public offer(ing)? of .*subsidiary|listing of .*subsidiary|"
     r"unlocking value", "Subsidiary listing", "Demerger/SOTP unlocking"),
    (r"amalgamation|merger of|merged with", "Amalgamation", "Demerger/SOTP unlocking"),
    (r"insolvency|nclt|resolution plan|corporate insolvency",
     "Insolvency / resolution", "Governance risk"),
    (r"qualified institutional placement|\bqip\b|preferential (issue|allotment)|"
     r"\bwarrants?\b", "Equity raise", "Governance risk"),
    (r"auditor.{0,40}(resign|qualif)|resignation of (statutory )?auditor",
     "Auditor change/qualification", "Governance risk"),
    (r"\bsebi\b.{0,60}(order|penalt|show cause|adjudicat)",
     "Regulatory action", "Governance risk"),
]


def nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
    })
    for u in ("https://www.nseindia.com",
              "https://www.nseindia.com/companies-listing/corporate-filings-announcements"):
        try:
            s.get(u, timeout=25)
            time.sleep(1.2)
        except requests.RequestException as e:
            print("  warmup:", e, file=sys.stderr)
    return s


def get_json(s: requests.Session, url: str, tries: int = 4):
    for a in range(tries):
        try:
            r = s.get(url, timeout=40)
            if r.status_code == 200 and r.text.strip().startswith(("[", "{")):
                return r.json()
            print(f"    HTTP {r.status_code} ({len(r.text)}b)", file=sys.stderr)
        except Exception as e:
            print(f"    {e}", file=sys.stderr)
        time.sleep(2.5 * (a + 1))
    return None


def fetch_window(s, frm: date, to: date) -> list:
    url = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           f"&from_date={frm.strftime('%d-%m-%Y')}&to_date={to.strftime('%d-%m-%Y')}")
    j = get_json(s, url)
    if isinstance(j, dict):
        j = j.get("data") or j.get("rows") or []
    return j or []


def classify_event(text: str):
    t = (text or "").lower()
    hits = []
    for pat, label, tag in EVENT_PATTERNS:
        if re.search(pat, t):
            hits.append((label, tag))
    return hits


def main() -> int:
    s = nse_session()
    today = date.today()

    # Announcements: walk back in 30-day windows over the last ~15 months.
    all_rows = []
    win = 30
    for k in range(15):
        to = today - timedelta(days=k * win)
        frm = to - timedelta(days=win)
        rows = fetch_window(s, frm, to)
        print(f"  {frm} -> {to}: {len(rows)} announcements", flush=True)
        all_rows.extend(rows)
        time.sleep(1.5)

    if not all_rows:
        print("No announcements retrieved - event archetype will stay under-detected",
              file=sys.stderr)
        pd.DataFrame().to_csv(RAW / "nse_announcements.csv", index=False)
        return 1

    ann = pd.DataFrame(all_rows)
    ann.to_csv(RAW / "nse_announcements.csv", index=False)
    print(f"\nTotal announcement rows: {len(ann)}  cols={list(ann.columns)[:10]}")

    symcol = next((c for c in ("symbol", "SYMBOL", "sym") if c in ann.columns), None)
    textcols = [c for c in ann.columns
                if c.lower() in ("desc", "subject", "attchmnttext", "smindustry",
                                 "attchmntfile", "sm_name", "descripion", "description")]
    if not symcol:
        print("No symbol column found", file=sys.stderr)
        return 1

    datecol = next((c for c in ann.columns
                    if c.lower() in ("an_dt", "sort_date", "exchdisstime", "dt")), None)

    flags = {}
    for r in ann.itertuples(index=False):
        sym = str(getattr(r, symcol, "")).strip()
        if not sym:
            continue
        blob = " ".join(str(getattr(r, c, "") or "") for c in textcols)
        for label, tag in classify_event(blob):
            key = (sym, label)
            when = str(getattr(r, datecol, "")) if datecol else ""
            if key not in flags or when > flags[key]["latest_date"]:
                flags[key] = {"symbol": sym, "event_class": label, "event_tag": tag,
                              "latest_date": when, "headline": blob[:220]}

    ef = pd.DataFrame(list(flags.values()))
    if not ef.empty:
        ef = ef.sort_values(["symbol", "latest_date"])
        ef.to_csv(RAW / "event_flags.csv", index=False)
        print(f"\nEvent flags: {len(ef)} across {ef['symbol'].nunique()} symbols")
        print(ef["event_class"].value_counts().to_string())
    else:
        print("No events matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
