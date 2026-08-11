"""
Phase 1 / Step 2 - Scrape screener.in public company pages for fundamentals.

Screener aggregates audited annual-report / quarterly-filing data. It is treated
here as a SECONDARY source: adequate for discovery and screening, but anything
material for a shortlisted name must later be confirmed against the filing
itself (that is Phase 2's job).

Resume-safe: each company is cached as data/fundamentals/<SYMBOL>.json and
skipped on re-run.

Usage:
    python scripts/02_fetch_fundamentals.py [--limit N] [--sleep 1.2]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from lxml import html as lhtml

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
FUND = ROOT / "data" / "fundamentals"
FUND.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

NUM_RE = re.compile(r"-?[\d,]+\.?\d*")


def to_num(txt: str | None):
    """'1,39,880' -> 139880.0 ; '48.8%' -> 48.8 ; '' -> None. Handles Indian commas."""
    if txt is None:
        return None
    t = txt.strip().replace(",", "").replace("₹", "").replace("%", "").replace("Cr.", "").strip()
    if t in ("", "-", "--", "NA", "N/A"):
        return None
    neg = t.startswith("-")
    m = NUM_RE.search(t.replace("-", "", 1) if neg else t)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


def parse_top_ratios(doc) -> dict:
    out = {}
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


def parse_section_table(doc, section_id: str) -> dict:
    """Parse a screener section table into {row_label: {period: value}}."""
    secs = doc.cssselect(f"section#{section_id} table.data-table")
    if not secs:
        return {}
    tbl = secs[0]
    heads = [" ".join(th.text_content().split())
             for th in tbl.cssselect("thead th")]
    periods = heads[1:] if heads else []
    out: dict[str, dict] = {}
    for tr in tbl.cssselect("tbody tr"):
        tds = tr.cssselect("td")
        if not tds:
            continue
        label = " ".join(tds[0].text_content().split())
        label = label.replace("\xa0", " ").rstrip("+").strip()
        if not label:
            continue
        vals = {}
        for p, td in zip(periods, tds[1:]):
            vals[p] = to_num(td.text_content())
        out[label] = vals
    return out


def parse_growth_tables(doc) -> dict:
    """Compounded sales/profit growth + stock CAGR + ROE tables under #profit-loss."""
    out = {}
    for tbl in doc.cssselect("section#profit-loss table.ranges-table"):
        head = tbl.cssselect("th")
        if not head:
            continue
        key = " ".join(head[0].text_content().split()).rstrip(":")
        rows = {}
        for tr in tbl.cssselect("tr")[1:]:
            tds = tr.cssselect("td")
            if len(tds) >= 2:
                rows[" ".join(tds[0].text_content().split()).rstrip(":")] = \
                    to_num(tds[1].text_content())
        out[key] = rows
    return out


def parse_about(doc) -> dict:
    out = {}
    ab = doc.cssselect("div.company-profile div.about p, section#company-info p")
    if ab:
        out["about"] = " ".join(ab[0].text_content().split())[:1200]
    links = doc.cssselect("div.company-links a, div.company-profile a")
    for a in links:
        t = " ".join(a.text_content().split())
        href = a.get("href", "")
        if "bseindia" in href:
            out["bse_url"] = href
        elif "nseindia" in href:
            out["nse_url"] = href
    peers = doc.cssselect("p.sub a[href*='/company/compare/'], section#peers a")
    for a in peers:
        href = a.get("href", "")
        if "/company/compare/" in href:
            out["sector_compare_url"] = href
            out["sector_label"] = " ".join(a.text_content().split())
            break
    return out


def scrape(sess: requests.Session, symbol: str) -> dict | None:
    urls = [f"https://www.screener.in/company/{symbol}/consolidated/",
            f"https://www.screener.in/company/{symbol}/"]
    last_status = None
    for u in urls:
        try:
            r = sess.get(u, timeout=40, allow_redirects=True)
        except requests.RequestException as e:
            last_status = f"ERR {e}"
            continue
        last_status = r.status_code
        if r.status_code != 200:
            continue
        if "/register/" in r.url or "/login/" in r.url:
            last_status = "GATED"
            continue
        doc = lhtml.fromstring(r.text)
        basis = "consolidated" if "consolidated" in r.url else "standalone"
        rec = {
            "symbol": symbol,
            "source_url": r.url,
            "basis": basis,
            "scraped_at": pd.Timestamp.utcnow().isoformat(),
            "name": " ".join(doc.cssselect("h1")[0].text_content().split())
                    if doc.cssselect("h1") else None,
            "top_ratios": parse_top_ratios(doc),
            "profit_loss": parse_section_table(doc, "profit-loss"),
            "balance_sheet": parse_section_table(doc, "balance-sheet"),
            "cash_flow": parse_section_table(doc, "cash-flow"),
            "ratios": parse_section_table(doc, "ratios"),
            "quarters": parse_section_table(doc, "quarters"),
            "shareholding": parse_section_table(doc, "shareholding"),
            "growth": parse_growth_tables(doc),
        }
        rec.update(parse_about(doc))
        if not rec["profit_loss"] and not rec["quarters"]:
            last_status = "NO_TABLES"
            continue
        return rec
    return {"symbol": symbol, "error": str(last_status)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.1)
    args = ap.parse_args()

    # Universe = every NSE series-EQ company (the full listed operating universe).
    # Index constituent lists are merged in only to attach NSE's own industry label.
    spine = pd.read_csv(RAW / "universe_spine.csv")
    spine["symbol"] = spine["symbol"].astype(str).str.strip()

    frames = []
    for f, tag in (("idx_niftytotalmarket.csv", "NiftyTotalMarket"),
                   ("idx_microcap250.csv", "NiftyMicrocap250"),
                   ("idx_nifty500.csv", "Nifty500")):
        p = RAW / f
        if p.exists():
            d = pd.read_csv(p)
            d.columns = [c.strip() for c in d.columns]
            d = d.rename(columns={"Company Name": "company_idx", "Industry": "industry",
                                  "Symbol": "symbol", "ISIN Code": "isin_idx"})
            d["frame"] = tag
            frames.append(d[["symbol", "industry", "frame"]])
    if frames:
        idxall = pd.concat(frames, ignore_index=True)
        idxall["symbol"] = idxall["symbol"].astype(str).str.strip()
        ind = idxall.drop_duplicates("symbol", keep="first")[["symbol", "industry"]]
        infr = (idxall.groupby("symbol")["frame"]
                .apply(lambda x: "|".join(sorted(set(x)))).reset_index()
                .rename(columns={"frame": "index_membership"}))
        spine = spine.merge(ind, on="symbol", how="left").merge(infr, on="symbol", how="left")
    else:
        spine["industry"] = None
        spine["index_membership"] = None

    idx = spine
    idx.to_csv(RAW / "universe_frame.csv", index=False)
    n_idx = idx["index_membership"].notna().sum()
    print(f"Universe frame: {len(idx)} NSE series-EQ symbols "
          f"({n_idx} carry an NSE index industry label)")

    todo = [s for s in idx["symbol"].tolist() if not (FUND / f"{s}.json").exists()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"Already cached: {len(idx) - len([s for s in idx['symbol'] if not (FUND/f'{s}.json').exists()])}"
          f" | To fetch now: {len(todo)}")

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})

    ok = err = 0
    t0 = time.time()
    for i, sym in enumerate(todo, 1):
        rec = scrape(sess, sym)
        (FUND / f"{sym}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        if rec and "error" not in rec:
            ok += 1
        else:
            err += 1
            print(f"  !! {sym}: {rec.get('error') if rec else 'None'}")
        if i % 25 == 0 or i == len(todo):
            el = time.time() - t0
            rate = i / el if el else 0
            eta = (len(todo) - i) / rate / 60 if rate else 0
            print(f"  [{i}/{len(todo)}] ok={ok} err={err} "
                  f"{rate:.2f}/s ETA {eta:.1f}min", flush=True)
        time.sleep(args.sleep + random.uniform(0, 0.5))

    print(f"\nDone. ok={ok} err={err}. Cache dir: {FUND}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
