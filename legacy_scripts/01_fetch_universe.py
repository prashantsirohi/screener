"""
Phase 1 / Step 1 - Build the NSE-listed equity universe spine.

Pulls the official NSE 'EQUITY_L' master list (all listed equity series) and the
latest full bhavcopy for traded prices + volumes. Both come from NSE's own
archives, so this is a primary-source universe rather than a scraped guess.

Outputs:
    data/raw/nse_equity_list.csv
    data/raw/nse_bhavcopy.csv
    data/raw/universe_spine.csv
"""

import io
import sys
import time
import datetime as dt
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def nse_session() -> requests.Session:
    """NSE requires a real browser handshake before archive files are served."""
    s = requests.Session()
    s.headers.update(HEADERS)
    for warmup in ("https://www.nseindia.com",
                   "https://www.nseindia.com/market-data/securities-available-for-trading"):
        try:
            s.get(warmup, timeout=20)
            time.sleep(1.0)
        except requests.RequestException as exc:
            print(f"  warmup {warmup} -> {exc}", file=sys.stderr)
    return s


def fetch(s: requests.Session, url: str, referer: str | None = None, tries: int = 4) -> bytes | None:
    h = {"Referer": referer} if referer else {}
    for attempt in range(1, tries + 1):
        try:
            r = s.get(url, headers=h, timeout=35)
            if r.status_code == 200 and r.content:
                return r.content
            print(f"  [{attempt}/{tries}] {url} -> HTTP {r.status_code}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"  [{attempt}/{tries}] {url} -> {exc}", file=sys.stderr)
        time.sleep(2.5 * attempt)
    return None


def get_equity_list(s: requests.Session) -> pd.DataFrame | None:
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    blob = fetch(s, url, referer="https://www.nseindia.com/market-data/securities-available-for-trading")
    if not blob:
        return None
    df = pd.read_csv(io.BytesIO(blob))
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    df.to_csv(RAW / "nse_equity_list.csv", index=False)
    return df


def get_bhavcopy(s: requests.Session, back_days: int = 12) -> tuple[pd.DataFrame | None, str | None]:
    """UDiFF full bhavcopy. Walk back from today to skip weekends/holidays."""
    today = dt.date.today()
    for i in range(back_days):
        d = today - dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y%m%d")
        url = (f"https://nsearchives.nseindia.com/content/cm/"
               f"BhavCopy_NSE_CM_0_0_0_{ds}_F_0000.csv.zip")
        blob = fetch(s, url, referer="https://www.nseindia.com/all-reports", tries=2)
        if blob:
            try:
                df = pd.read_csv(io.BytesIO(blob), compression="zip")
            except Exception:
                df = pd.read_csv(io.BytesIO(blob))
            df.columns = [c.strip().upper() for c in df.columns]
            df.to_csv(RAW / "nse_bhavcopy.csv", index=False)
            print(f"  bhavcopy date = {d.isoformat()}  rows={len(df)}")
            return df, d.isoformat()
    return None, None


def main() -> int:
    print("Opening NSE session ...")
    s = nse_session()

    print("Fetching EQUITY_L master list ...")
    eq = get_equity_list(s)
    if eq is None:
        print("FAILED: could not retrieve NSE equity list", file=sys.stderr)
        return 2
    print(f"  equity list rows = {len(eq)}  cols = {list(eq.columns)[:8]}")

    print("Fetching latest bhavcopy ...")
    bhav, bhav_date = get_bhavcopy(s)
    if bhav is None:
        print("WARNING: bhavcopy unavailable; universe will lack traded price/volume",
              file=sys.stderr)

    eq_eq = eq[eq.get("SERIES", pd.Series(dtype=str)).astype(str).str.strip() == "EQ"].copy()
    print(f"  series==EQ rows = {len(eq_eq)}")

    spine = eq_eq.rename(columns={
        "SYMBOL": "symbol",
        "NAME_OF_COMPANY": "company",
        "ISIN_NUMBER": "isin",
        "DATE_OF_LISTING": "date_of_listing",
        "FACE_VALUE": "face_value",
    })[["symbol", "company", "isin", "date_of_listing", "face_value"]]
    spine["symbol"] = spine["symbol"].astype(str).str.strip()
    spine["company"] = spine["company"].astype(str).str.strip()

    if bhav is not None:
        cols = set(bhav.columns)
        # UDiFF schema vs legacy schema
        if {"TCKRSYMB", "CLSPRIC"} <= cols:
            b = bhav[bhav.get("SCTYSRS", "EQ").astype(str).str.strip() == "EQ"].copy()
            b = b.rename(columns={"TCKRSYMB": "symbol", "CLSPRIC": "close_inr",
                                  "TTLTRADVAL": "turnover_inr", "TTLTRFVOL": "volume"})
        elif {"SYMBOL", "CLOSE"} <= cols:
            b = bhav[bhav["SERIES"].astype(str).str.strip() == "EQ"].copy()
            b = b.rename(columns={"SYMBOL": "symbol", "CLOSE": "close_inr",
                                  "TOTTRDVAL": "turnover_inr", "TOTTRDQTY": "volume"})
        else:
            b = None
        if b is not None:
            keep = [c for c in ["symbol", "close_inr", "turnover_inr", "volume"] if c in b.columns]
            b = b[keep].copy()
            b["symbol"] = b["symbol"].astype(str).str.strip()
            b = b.drop_duplicates("symbol")
            spine = spine.merge(b, on="symbol", how="left")
            spine["price_date"] = bhav_date

    spine.to_csv(RAW / "universe_spine.csv", index=False)
    print(f"\nWrote {RAW / 'universe_spine.csv'}  rows={len(spine)}")
    print(spine.head(8).to_string())
    priced = spine["close_inr"].notna().sum() if "close_inr" in spine.columns else 0
    print(f"\nrows with traded close price: {priced}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
