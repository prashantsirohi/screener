"""Re-run event classification over the cached announcements (no refetch)."""
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from importlib import import_module
mod = import_module("05_fetch_announcements") if False else None

# import the patterns directly to avoid the numeric module-name problem
import re
exec(open(Path(__file__).parent / "05_fetch_announcements.py", encoding="utf-8")
     .read().split("def nse_session")[0].split("EVENT_PATTERNS = ")[1].join(["EVENT_PATTERNS = ", ""]))

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

ann = pd.read_csv(RAW / "nse_announcements.csv", low_memory=False)
print("announcement rows:", len(ann))

symcol = "symbol"
textcols = [c for c in ann.columns if c.lower() in ("desc", "attchmnttext", "smindustry")]
datecol = "an_dt" if "an_dt" in ann.columns else "dt"
print("using text columns:", textcols)


def classify_event(text: str):
    t = (text or "").lower()
    return [(label, tag) for pat, label, tag in EVENT_PATTERNS if re.search(pat, t)]


flags = {}
for r in ann.itertuples(index=False):
    sym = str(getattr(r, symcol, "") or "").strip()
    if not sym or sym == "nan":
        continue
    blob = " ".join(str(getattr(r, c, "") or "") for c in textcols)
    when = str(getattr(r, datecol, "") or "")
    for label, tag in classify_event(blob):
        key = (sym, label)
        if key not in flags or when > flags[key]["latest_date"]:
            flags[key] = {"symbol": sym, "event_class": label, "event_tag": tag,
                          "latest_date": when, "headline": blob[:220]}

ef = pd.DataFrame(list(flags.values())).sort_values(["symbol", "latest_date"])
ef.to_csv(RAW / "event_flags.csv", index=False)
print(f"\nEvent flags: {len(ef)} across {ef['symbol'].nunique()} symbols")
print(ef["event_class"].value_counts().to_string())
print("\nSample demerger/asset-sale headlines:")
sample = ef[ef["event_class"].isin(["Demerger / scheme of arrangement", "Asset / business sale",
                                    "Subsidiary listing", "Capital reduction"])].head(8)
for r in sample.itertuples(index=False):
    print(f"  {r.symbol:<12} {r.event_class:<34} {r.latest_date[:11]} :: {r.headline[:90]}")
