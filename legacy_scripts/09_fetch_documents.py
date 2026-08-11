"""
Phase 2 / Step 1 - collect PRIMARY document links for shortlisted companies.

screener.in's documents block links straight through to the filings themselves:
BSE-hosted annual report PDFs, quarterly result filings, board-meeting outcomes,
and CARE/CRISIL/ICRA rating rationales. Those are primary sources, and they are
what Phase 2 must verify material claims against.

Also captures the concall block (transcripts / presentations) where present.

Usage:
    python scripts/09_fetch_documents.py --symbols-file phase2/shortlist.csv
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from pathlib import Path

import pandas as pd
import requests
from lxml import html as lhtml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "documents"
DOCS.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def classify_doc(text: str, href: str) -> str:
    t = (text or "").lower()
    h = (href or "").lower()
    if re.search(r"financial year \d{4}", t) or "annualreport" in h:
        return "Annual report"
    if "rating" in t or "careratings" in h or "crisil" in h or "icra" in h:
        return "Credit rating rationale"
    if "results" in t or "financial results" in t:
        return "Quarterly/annual results filing"
    if "transcript" in t:
        return "Earnings call transcript"
    if "ppt" in t or "presentation" in t:
        return "Investor presentation"
    if "notes" in t:
        return "Concall notes"
    if "board meeting" in t:
        return "Board meeting intimation/outcome"
    if "drhp" in t or "sebi.gov.in" in h:
        return "Offer document"
    if "annual report" in t:
        return "Annual report"
    return "Exchange filing"


def fy_of(text: str) -> str | None:
    m = re.search(r"financial year (\d{4})", (text or "").lower())
    if m:
        return f"FY{m.group(1)}"
    m = re.search(r"(20\d{2})", text or "")
    return m.group(1) if m else None


def scrape_docs(sess: requests.Session, symbol: str) -> dict:
    for u in (f"https://www.screener.in/company/{symbol}/consolidated/",
              f"https://www.screener.in/company/{symbol}/"):
        try:
            r = sess.get(u, timeout=40, allow_redirects=True)
        except requests.RequestException as e:
            return {"symbol": symbol, "error": str(e)}
        if r.status_code != 200 or "/register/" in r.url:
            continue
        d = lhtml.fromstring(r.text)
        out = []
        for sec_id in ("documents", "concalls"):
            for sec in d.cssselect(f"section#{sec_id}"):
                for a in sec.cssselect("a"):
                    href = a.get("href", "")
                    if not href or href.startswith("#"):
                        continue
                    txt = " ".join(a.text_content().split())
                    if not txt:
                        continue
                    out.append({
                        "text": txt[:200],
                        "url": href if href.startswith("http") else f"https://www.screener.in{href}",
                        "doc_type": classify_doc(txt, href),
                        "period": fy_of(txt),
                        "block": sec_id,
                    })
        # de-duplicate on url
        seen, docs = set(), []
        for x in out:
            if x["url"] not in seen:
                seen.add(x["url"])
                docs.append(x)
        return {"symbol": symbol, "source_page": r.url, "documents": docs,
                "fetched_at": pd.Timestamp.now(tz="UTC").isoformat()}
    return {"symbol": symbol, "error": "unreachable"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", required=True)
    ap.add_argument("--sleep", type=float, default=1.2)
    args = ap.parse_args()

    syms = pd.read_csv(args.symbols_file)["symbol"].astype(str).str.strip().tolist()
    todo = [s for s in syms if not (DOCS / f"{s}.json").exists()]
    print(f"{len(syms)} symbols | {len(todo)} to fetch")

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    rows = []
    for i, sym in enumerate(todo, 1):
        rec = scrape_docs(sess, sym)
        (DOCS / f"{sym}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        n = len(rec.get("documents", []))
        print(f"  [{i}/{len(todo)}] {sym:<12} {n} docs"
              + (f"  ERR {rec['error']}" if rec.get("error") else ""), flush=True)
        time.sleep(args.sleep + random.uniform(0, 0.4))

    # flat index across everything cached
    for p in DOCS.glob("*.json"):
        rec = json.loads(p.read_text(encoding="utf-8"))
        for d in rec.get("documents", []):
            rows.append({"symbol": rec["symbol"], **d})
    if rows:
        idx = pd.DataFrame(rows)
        idx.to_csv(ROOT / "data" / "raw" / "document_index.csv", index=False)
        print(f"\nDocument index: {len(idx)} links across {idx['symbol'].nunique()} companies")
        print(idx["doc_type"].value_counts().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
