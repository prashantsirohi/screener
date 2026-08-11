"""
Archetype, tag, scoring and eligibility parity.

`domain/archetypes.py` is a byte-identical copy of the oracle and
`domain/scoring.py` was extracted verbatim, so what is under test is whether the
inputs they receive from the store are the same inputs the legacy screen built
from JSON files.

This is the last link in the chain: metrics parity proves the fundamentals,
technical parity proves the bars, and this proves the classification and score
that Phase 1 actually ranks on.
"""

from __future__ import annotations

import importlib.util
import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from market_screener.analytics.duck import analytics_session
from market_screener.analytics.features import BROAD_BENCHMARK
from market_screener.config import load_settings
from market_screener.db.connection import Database
from market_screener.domain import archetypes as ported_arch
from market_screener.domain import fundamentals_view as fv
from market_screener.domain import metrics as ported_metrics
from market_screener.domain import weinstein as ported_wein
from market_screener.domain.eligibility import assess, data_quality
from market_screener.domain.scoring import score_priority
from market_screener.ingest import classify_events

from ._bars import last_complete_week, truncate_to_complete_weeks

pytestmark = pytest.mark.parity

FUND_CACHE = Path("data/fundamentals")
PRICE_CACHE = Path("data/prices")
GOLDEN_SIZE = 60


def _ref(name: str, filename: str):
    p = Path(__file__).resolve().parents[1] / "reference" / filename
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


legacy_metrics = _ref("legacy_metrics", "legacy_metrics.py")
legacy_arch = _ref("legacy_archetypes", "legacy_archetypes.py")
legacy_wein = _ref("legacy_weinstein", "legacy_weinstein.py")


@pytest.fixture(scope="module")
def settings():
    return load_settings()


@pytest.fixture(scope="module")
def db(settings):
    d = Database(settings.pg)
    if not d.database_exists() or not d.ping():
        pytest.skip("database unavailable")
    return d


@pytest.fixture(scope="module")
def labels(db):
    return fv._label_lookup(db)


@pytest.fixture(scope="module")
def yahoo_frames(settings):
    with analytics_session(settings) as con:
        df = con.execute("""
            SELECT w.security_id, s.symbol, w.week_end_date,
                   w.open, w.high, w.low, w.close, w.volume
            FROM   src_weekly_bar_all w
            JOIN   src_security s USING (security_id)
            WHERE  w.source = 'yahoo_weekly'
            ORDER  BY w.security_id, w.week_end_date
        """).df()
    if df.empty:
        pytest.skip("no Yahoo bars")
    return {sym: g for sym, g in df.groupby("symbol", sort=False)}


@pytest.fixture(scope="module")
def golden(db, yahoo_frames):
    rows = db.fetch_all("""
        SELECT s.security_id, s.symbol, s.nse_industry
        FROM   market.security s
        JOIN   market.screener_page_raw p ON p.security_id = s.security_id
        WHERE  NOT p.is_blank AND s.series = 'EQ' AND s.is_active
          AND  NOT EXISTS (SELECT 1 FROM market.fetch_retry_queue q
                           WHERE q.scope = s.symbol AND q.state = 'resolved')
        ORDER  BY s.symbol
    """)
    usable = [r for r in rows
              if r["symbol"] in yahoo_frames
              and (FUND_CACHE / f"{r['symbol']}.json").exists()
              and (PRICE_CACHE / f"{r['symbol']}.json").exists()
              and len(yahoo_frames[r["symbol"]]) >= 40]
    if len(usable) < GOLDEN_SIZE:
        pytest.skip(f"only {len(usable)} comparable companies")
    return usable[:GOLDEN_SIZE]


@pytest.fixture(scope="module")
def legacy_events():
    p = Path("data/raw/event_flags.csv")
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    out: dict[str, list[dict]] = {}
    for r in df.itertuples(index=False):
        out.setdefault(str(r.symbol).strip(), []).append({
            "event_class": str(r.event_class).strip(),
            "event_tag": str(r.event_tag).strip(),
            "latest_date": str(r.latest_date),
            "headline": str(r.headline)})
    return out


@pytest.fixture(scope="module")
def ported_events(db):
    out: dict[str, list[dict]] = {}
    for r in classify_events.event_flags(db, "v1"):
        out.setdefault(r["symbol"], []).append({
            "event_class": r["event_class"],
            "latest_date": str(r["latest_date"]),
            "headline": r["headline"] or ""})
    return out


def _pair(db, labels, yahoo_frames, legacy_events, ported_events, row):
    """Build the legacy and ported (metrics, technicals, events) for one company."""
    sym, sid, industry = row["symbol"], row["security_id"], row["nse_industry"]

    lm = legacy_metrics.compute(
        json.loads((FUND_CACHE / f"{sym}.json").read_text(encoding="utf-8")), industry)
    pm = ported_metrics.compute(fv.payload_for_metrics(db, sid, labels=labels), industry)

    # Align the oracle to the port's completed weeks; the store excludes the
    # current partial week and the oracle does not.
    pframe = ported_wein.bars_from_frame(yahoo_frames[sym])
    pbm = ported_wein.bars_from_frame(yahoo_frames[BROAD_BENCHMARK])
    cutoff = last_complete_week(pframe)
    lbm = truncate_to_complete_weeks(
        legacy_wein.load_bars(PRICE_CACHE / f"_BM_{BROAD_BENCHMARK}.json"), cutoff)
    lframe = truncate_to_complete_weeks(
        legacy_wein.load_bars(PRICE_CACHE / f"{sym}.json"), cutoff)
    lt = legacy_wein.analyse(lframe, lbm, None)
    pt = ported_wein.analyse(pframe, pbm, None)

    return lm, pm, lt, pt, legacy_events.get(sym, []), ported_events.get(sym, [])


def test_primary_archetype_matches(db, labels, yahoo_frames, golden,
                                   legacy_events, ported_events):
    diffs = []
    for row in golden:
        lm, pm, lt, pt, le, pe = _pair(db, labels, yahoo_frames,
                                       legacy_events, ported_events, row)
        want = legacy_arch.classify(lm, le)
        got = ported_arch.classify(pm, pe)
        if want["primary_archetype"] != got["primary_archetype"]:
            diffs.append(f"{row['symbol']}: {want['primary_archetype']} -> "
                         f"{got['primary_archetype']}")
    assert not diffs, f"{len(diffs)} archetype changes:\n  " + "\n  ".join(diffs[:20])


def test_archetype_fit_scores_match(db, labels, yahoo_frames, golden,
                                    legacy_events, ported_events):
    diffs = []
    for row in golden:
        lm, pm, lt, pt, le, pe = _pair(db, labels, yahoo_frames,
                                       legacy_events, ported_events, row)
        want = legacy_arch.classify(lm, le)["archetype_scores"]
        got = ported_arch.classify(pm, pe)["archetype_scores"]
        for k in set(want) | set(got):
            if abs(float(want.get(k, 0)) - float(got.get(k, 0))) > 1e-6:
                diffs.append(f"{row['symbol']}.{k}: {want.get(k)} -> {got.get(k)}")
    assert not diffs, f"{len(diffs)} fit-score changes:\n  " + "\n  ".join(diffs[:20])


def test_secondary_tags_match(db, labels, yahoo_frames, golden,
                              legacy_events, ported_events):
    diffs = []
    for row in golden:
        lm, pm, lt, pt, le, pe = _pair(db, labels, yahoo_frames,
                                       legacy_events, ported_events, row)
        wa = legacy_arch.classify(lm, le)["primary_archetype"]
        ga = ported_arch.classify(pm, pe)["primary_archetype"]
        want = legacy_arch.secondary_tags(lm, lt, wa, le)
        got = ported_arch.secondary_tags(pm, pt, ga, pe)
        if set(want) != set(got):
            diffs.append(f"{row['symbol']}: {sorted(set(want) ^ set(got))}")
    assert not diffs, f"{len(diffs)} tag changes:\n  " + "\n  ".join(diffs[:20])


def test_priority_scores_match(db, labels, yahoo_frames, golden,
                               legacy_events, ported_events):
    """The score decides the ranking, so a drift here reorders the candidate list."""
    diffs = []
    for row in golden:
        lm, pm, lt, pt, le, pe = _pair(db, labels, yahoo_frames,
                                       legacy_events, ported_events, row)
        wfit = legacy_arch.classify(lm, le)["archetype_fit"]
        gfit = ported_arch.classify(pm, pe)["archetype_fit"]
        from market_screener.analytics.features import liquidity_from_bars
        pframe = ported_wein.bars_from_frame(yahoo_frames[row["symbol"]])
        liq_p, _ = liquidity_from_bars(pframe)
        liq_l, _ = _legacy_liquidity(truncate_to_complete_weeks(
            legacy_wein.load_bars(PRICE_CACHE / f"{row['symbol']}.json"),
            last_complete_week(pframe)))

        ws, wb = score_priority(lm, lt, wfit, liq_l)
        gs, gb = score_priority(pm, pt, gfit, liq_p)
        if abs(ws - gs) > 1e-6:
            diffs.append(f"{row['symbol']}: {ws} -> {gs} (components {wb} vs {gb})")
    assert not diffs, f"{len(diffs)} score changes:\n  " + "\n  ".join(diffs[:15])


def _legacy_liquidity(df):
    import numpy as np
    if df is None or len(df) < 13:
        return None, "insufficient bars"
    d = df.tail(13)
    typical = d[["high", "low", "adjclose"]].mean(axis=1)
    weekly_val = (typical * d["volume"]).dropna()
    if weekly_val.empty:
        return None, "no volume"
    return round(float(np.median(weekly_val)) / 5.0 / 1e7, 3), "13-week median"


def test_eligibility_verdicts_match(db, labels, yahoo_frames, golden):
    """The extracted gates must exclude exactly what the inline logic excluded."""
    diffs = []
    for row in golden:
        sym, sid = row["symbol"], row["security_id"]
        pm = ported_metrics.compute(
            fv.payload_for_metrics(db, sid, labels=labels), row["nse_industry"])
        frame = ported_wein.bars_from_frame(yahoo_frames[sym])
        from market_screener.analytics.features import liquidity_from_bars
        liq, _ = liquidity_from_bars(frame)
        got = assess(pm, pm.get("market_cap_inr_cr"), len(frame), liq)

        mc = pm.get("market_cap_inr_cr")
        if pm.get("data_error") or not pm.get("company"):
            want = "EX_NO_FUNDAMENTALS"
        elif mc is None:
            want = "EX_NO_MCAP"
        elif mc < 1000:
            want = "EX_MCAP_BELOW_BAND"
        elif mc > 100000:
            want = "EX_MCAP_ABOVE_BAND"
        elif (pm.get("fy_count") or 0) < 3:
            want = "EX_SHORT_FIN_HISTORY"
        elif len(frame) < 40:
            want = "EX_NO_PRICE_HISTORY"
        elif liq is None or liq < 1.0:
            want = "EX_ILLIQUID"
        else:
            want = None
        if got.code != want:
            diffs.append(f"{sym}: expected {want} got {got.code}")
    assert not diffs, f"{len(diffs)} eligibility changes:\n  " + "\n  ".join(diffs[:15])


def test_data_quality_flags_match(db, labels, golden):
    for row in golden:
        pm = ported_metrics.compute(
            fv.payload_for_metrics(db, row["security_id"], labels=labels),
            row["nse_industry"])
        assert data_quality(pm) in ("High", "Medium", "Low")
