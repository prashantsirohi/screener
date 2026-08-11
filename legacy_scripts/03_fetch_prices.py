"""
Phase 1 / Step 3 - Pull split/dividend-adjusted WEEKLY OHLCV for the universe.

Uses the Yahoo Finance chart endpoint, which returns an `adjclose` series
alongside raw OHLC. Weinstein stage work requires adjusted weekly data, so the
adjusted series is what the classifier consumes. Benchmarks (Nifty 500 for broad
RS, Nifty 50 as fallback) are pulled the same way.

Resume-safe: cached per symbol at data/prices/<SYMBOL>.json.

Usage:
    python scripts/03_fetch_prices.py [--limit N] [--sleep 0.6] [--range 5y]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PRICES = ROOT / "data" / "prices"
PRICES.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BENCHMARKS = {
    "^CRSLDX": "NIFTY_500",
    "^NSEI": "NIFTY_50",
    "^CNXIT": "NIFTY_IT",
    "^NSEBANK": "NIFTY_BANK",
    "^CNXPHARMA": "NIFTY_PHARMA",
    "^CNXAUTO": "NIFTY_AUTO",
    "^CNXFMCG": "NIFTY_FMCG",
    "^CNXMETAL": "NIFTY_METAL",
    "^CNXENERGY": "NIFTY_ENERGY",
    "^CNXINFRA": "NIFTY_INFRA",
    "^CNXREALTY": "NIFTY_REALTY",
    "^CNXPSUBANK": "NIFTY_PSUBANK",
}


def fetch_chart(sess, yf_symbol: str, rng: str, tries: int = 3) -> dict | None:
    for host in ("query1", "query2"):
        for attempt in range(tries):
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
                   f"?range={rng}&interval=1wk&events=div%2Csplit")
            try:
                r = sess.get(url, timeout=30)
                if r.status_code == 200:
                    j = r.json()
                    res = (j.get("chart") or {}).get("result")
                    if not res:
                        return None
                    res = res[0]
                    ts = res.get("timestamp") or []
                    if len(ts) < 30:
                        return None
                    q = res["indicators"]["quote"][0]
                    adj = None
                    if res["indicators"].get("adjclose"):
                        adj = res["indicators"]["adjclose"][0].get("adjclose")
                    return {
                        "symbol": yf_symbol,
                        "currency": res["meta"].get("currency"),
                        "timestamp": ts,
                        "open": q.get("open"), "high": q.get("high"),
                        "low": q.get("low"), "close": q.get("close"),
                        "volume": q.get("volume"), "adjclose": adj,
                        "fetched_at": pd.Timestamp.now(tz="UTC").isoformat(),
                    }
                if r.status_code in (404, 401):
                    return None
            except requests.RequestException:
                pass
            time.sleep(1.2 * (attempt + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.6)
    ap.add_argument("--range", default="5y")
    ap.add_argument("--symbols-file", default=None,
                    help="optional CSV with a 'symbol' column to restrict the run")
    args = ap.parse_args()

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json"})

    # Benchmarks first - the classifier cannot compute RS without them.
    for yfs, name in BENCHMARKS.items():
        out = PRICES / f"_BM_{name}.json"
        if out.exists():
            continue
        rec = fetch_chart(sess, yfs, args.range)
        if rec:
            rec["benchmark_name"] = name
            out.write_text(json.dumps(rec), encoding="utf-8")
            print(f"  benchmark {name:<14} bars={len(rec['timestamp'])}")
        else:
            print(f"  benchmark {name:<14} FAILED ({yfs})")
        time.sleep(args.sleep)

    src = Path(args.symbols_file) if args.symbols_file else (RAW / "universe_frame.csv")
    uni = pd.read_csv(src)
    syms = [str(s).strip() for s in uni["symbol"].dropna().tolist()]

    todo = [s for s in syms if not (PRICES / f"{s}.json").exists()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"Universe {len(syms)} | to fetch {len(todo)}")

    ok = miss = 0
    t0 = time.time()
    for i, sym in enumerate(todo, 1):
        rec = fetch_chart(sess, f"{sym}.NS", args.range)
        if rec is None:
            (PRICES / f"{sym}.json").write_text(
                json.dumps({"symbol": sym, "error": "NO_DATA"}), encoding="utf-8")
            miss += 1
        else:
            rec["nse_symbol"] = sym
            (PRICES / f"{sym}.json").write_text(json.dumps(rec), encoding="utf-8")
            ok += 1
        if i % 50 == 0 or i == len(todo):
            el = time.time() - t0
            rate = i / el if el else 0
            print(f"  [{i}/{len(todo)}] ok={ok} miss={miss} {rate:.2f}/s "
                  f"ETA {(len(todo)-i)/rate/60 if rate else 0:.1f}min", flush=True)
        time.sleep(args.sleep + random.uniform(0, 0.3))

    print(f"\nDone. ok={ok} miss={miss}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
