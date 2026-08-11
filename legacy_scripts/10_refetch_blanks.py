"""
Re-fetch companies whose cached screener page came back as an empty template.

During the bulk scrape screener throttled some requests and returned the page
skeleton with every numeric span blank. Those records parse without error but
carry no data, so they must be identified structurally (not by error flag) and
re-fetched at a slower rate.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parents[1]
FUND = ROOT / "data" / "fundamentals"


def is_blank(rec: dict) -> bool:
    if rec.get("error"):
        return True
    tr = rec.get("top_ratios") or {}
    if any(v is not None for v in tr.values()):
        return False
    pl = rec.get("profit_loss") or {}
    for row in pl.values():
        if any(v is not None for v in row.values()):
            return False
    return True


def main() -> int:
    sleep = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    blanks = []
    for p in sorted(FUND.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            blanks.append(p.stem)
            continue
        if is_blank(rec):
            blanks.append(p.stem)
    print(f"blank/empty cached records: {len(blanks)}")
    if not blanks:
        return 0

    # import the scraper's parser
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fetchfund", Path(__file__).parent / "02_fetch_fundamentals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.screener.in/",
    })
    try:
        sess.get("https://www.screener.in/", timeout=30)
        time.sleep(2)
    except requests.RequestException:
        pass

    fixed = still = 0
    for i, sym in enumerate(blanks, 1):
        rec = mod.scrape(sess, sym)
        if rec and not is_blank(rec):
            (FUND / f"{sym}.json").write_text(json.dumps(rec, ensure_ascii=False),
                                              encoding="utf-8")
            fixed += 1
            status = "OK"
        else:
            still += 1
            status = "still blank"
        if i % 20 == 0 or i == len(blanks):
            print(f"  [{i}/{len(blanks)}] fixed={fixed} still_blank={still}", flush=True)
        elif status != "OK":
            pass
        time.sleep(sleep + random.uniform(0, 1.5))

    print(f"\nRecovered {fixed}; {still} remain unavailable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
