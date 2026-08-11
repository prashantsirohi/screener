"""
Technical parity: the ported Weinstein layer must reproduce the frozen oracle
exactly.

`domain/weinstein.py` is a verbatim port apart from its data loader, so this
tests the loader and the plumbing: does reading bars out of Postgres produce the
same frame the JSON files did, and therefore the same stage?

Comparison is pinned to the Yahoo-sourced bars, because that is what the legacy
pipeline read. The bhavcopy series is a different (price-return) basis and is
expected to differ; that cutover is measured separately, not smuggled in here.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pandas as pd
import pytest

from market_screener.analytics.duck import analytics_session
from market_screener.analytics.features import BROAD_BENCHMARK, SECTOR_BENCHMARK
from market_screener.config import load_settings
from market_screener.domain import weinstein as ported

from ._bars import last_complete_week, truncate_to_complete_weeks

pytestmark = pytest.mark.parity

PRICE_CACHE = Path("data/prices")
GOLDEN_SIZE = 60
TOL = 1e-6


def _load_reference():
    p = Path(__file__).resolve().parents[1] / "reference" / "legacy_weinstein.py"
    spec = importlib.util.spec_from_file_location("legacy_weinstein", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


legacy = _load_reference()


@pytest.fixture(scope="module")
def settings():
    return load_settings()


@pytest.fixture(scope="module")
def yahoo_bars(settings):
    """All Yahoo-sourced weekly bars, matching what the legacy pipeline read."""
    with analytics_session(settings) as con:
        res = con.execute("""
            SELECT w.security_id, s.symbol, w.week_end_date,
                   w.open, w.high, w.low, w.close, w.volume
            FROM   src_weekly_bar_all w
            JOIN   src_security s USING (security_id)
            WHERE  w.source = 'yahoo_weekly'
            ORDER  BY w.security_id, w.week_end_date
        """).df()
    if res.empty:
        pytest.skip("no Yahoo weekly bars in the store")
    return res


@pytest.fixture(scope="module")
def frames(yahoo_bars):
    return {sym: g for sym, g in yahoo_bars.groupby("symbol", sort=False)}


@pytest.fixture(scope="module")
def golden(frames):
    syms = sorted(s for s in frames
                  if not s.startswith("NIFTY_")
                  and (PRICE_CACHE / f"{s}.json").exists()
                  and len(frames[s]) >= 40)
    if len(syms) < GOLDEN_SIZE:
        pytest.skip(f"only {len(syms)} comparable symbols")
    return syms[:GOLDEN_SIZE]


@pytest.fixture(scope="module")
def benches(frames):
    bm = ported.bars_from_frame(frames[BROAD_BENCHMARK]) if BROAD_BENCHMARK in frames else None
    legacy_bm = legacy.load_bars(PRICE_CACHE / f"_BM_{BROAD_BENCHMARK}.json")
    # The benchmark must be truncated to the same weeks as the stocks, or the
    # relative-strength join compares series of different lengths.
    if bm is not None and legacy_bm is not None:
        legacy_bm = truncate_to_complete_weeks(legacy_bm, last_complete_week(bm))
    return bm, legacy_bm


def _legacy_aligned(sym: str, ported_frame):
    """Oracle frame for `sym`, truncated to the port's completed weeks."""
    raw = legacy.load_bars(PRICE_CACHE / f"{sym}.json")
    if raw is None or ported_frame is None:
        return raw
    return truncate_to_complete_weeks(raw, last_complete_week(ported_frame))


def _same(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(float(a)) and math.isnan(float(b)):
            return True
        return abs(float(a) - float(b)) <= TOL * max(1.0, abs(float(a)), abs(float(b)))
    return a == b


# Provenance, not analysis.
SKIP_FIELDS = {"security_id", "symbol", "benchmark_symbol", "sector_benchmark",
               "bar_source", "adj_basis", "liquidity_inr_cr", "liquidity_period"}

# Date labels are expected to move, and only these. Yahoo stamps a weekly bar at
# week-START (Monday); the store normalises every bar to the ISO week's Friday so
# that a bhavcopy-derived series and a Yahoo-derived one land on the same key.
# Without it the stock/benchmark join drops nearly every row and relative
# strength silently comes back NULL.
#
# These are not skipped - test_date_fields_shift_by_exactly_one_iso_week asserts
# the shift is precisely Monday-to-Friday of the same week and nothing else.
DATE_FIELDS = {"technical_data_date", "base_start_date", "base_end_date"}


def test_bar_frames_match_the_legacy_loader(frames, golden):
    """The frame the store produces must equal the one the JSON reader produced."""
    bad = []
    for sym in golden:
        got = ported.bars_from_frame(frames[sym])
        want = _legacy_aligned(sym, got)
        if want is None or got is None:
            bad.append((sym, "one side is None"))
            continue
        if len(want) != len(got):
            bad.append((sym, f"length {len(want)} vs {len(got)}"))
            continue
        for col in ("adjclose", "high", "low", "volume"):
            w = pd.to_numeric(want[col], errors="coerce").reset_index(drop=True)
            g = pd.to_numeric(got[col], errors="coerce").reset_index(drop=True)
            if not ((w - g).abs().fillna(0) <= 1e-6 * w.abs().clip(lower=1)).all():
                bad.append((sym, f"{col} differs"))
                break
    assert not bad, f"frame mismatches: {bad[:8]}"


def test_stage_and_every_field_match(frames, golden, benches):
    ported_bm, legacy_bm = benches
    failures, compared = [], 0

    for sym in golden:
        ported_frame = ported.bars_from_frame(frames[sym])
        want = legacy.analyse(_legacy_aligned(sym, ported_frame), legacy_bm, None)
        got = ported.analyse(ported_frame, ported_bm, None)

        for k in sorted((set(want) | set(got)) - SKIP_FIELDS - DATE_FIELDS):
            compared += 1
            if not _same(want.get(k), got.get(k)):
                failures.append(f"{sym}.{k}: legacy={want.get(k)!r} port={got.get(k)!r}")

    assert compared > GOLDEN_SIZE * 20, f"only {compared} fields compared"
    assert not failures, (
        f"{len(failures)} mismatches over {len(golden)} symbols:\n  "
        + "\n  ".join(failures[:25]))


def test_date_fields_shift_by_exactly_one_iso_week(frames, golden, benches):
    """
    The only permitted difference, pinned down.

    Every date the port reports must be the Friday of the same ISO week as the
    date the oracle reported. That confirms the change is the intended week
    normalisation rather than an off-by-one that happens to look like one.
    """
    from datetime import date as _date, timedelta

    ported_bm, legacy_bm = benches
    problems = []

    for sym in golden:
        ported_frame = ported.bars_from_frame(frames[sym])
        want = legacy.analyse(_legacy_aligned(sym, ported_frame), legacy_bm, None)
        got = ported.analyse(ported_frame, ported_bm, None)

        for k in DATE_FIELDS:
            w, g = want.get(k), got.get(k)
            if w is None and g is None:
                continue
            if w is None or g is None:
                problems.append(f"{sym}.{k}: one side missing ({w!r} vs {g!r})")
                continue
            wd = _date.fromisoformat(str(w))
            gd = _date.fromisoformat(str(g))
            expected = wd + timedelta(days=4 - wd.weekday())
            if gd != expected:
                problems.append(
                    f"{sym}.{k}: {w} -> {g}, expected the same week's Friday {expected}")
            if gd.weekday() != 4:
                problems.append(f"{sym}.{k}: {g} is not a Friday")

    assert not problems, (
        f"{len(problems)} date shifts are not the ISO-Friday normalisation:\n  "
        + "\n  ".join(problems[:20]))


def test_stages_are_from_the_controlled_vocabulary(frames, golden, benches):
    ported_bm, _ = benches
    valid = set(ported.STAGES)
    for sym in golden:
        rec = ported.analyse(ported.bars_from_frame(frames[sym]), ported_bm, None)
        assert rec["technical_stage"] in valid, (sym, rec["technical_stage"])


def test_relative_strength_is_actually_computed(frames, golden, benches):
    """
    Guards the silent-null path: rs_vs returns None when the stock/benchmark join
    keeps fewer than 30 rows, so a week-alignment regression would show up as
    missing RS rather than an error.
    """
    ported_bm, _ = benches
    have_rs = 0
    for sym in golden:
        rec = ported.analyse(ported.bars_from_frame(frames[sym]), ported_bm, None)
        if rec.get("rs_bm_13w_pct") is not None:
            have_rs += 1
    assert have_rs >= len(golden) * 0.9, (
        f"relative strength computed for only {have_rs}/{len(golden)} - "
        f"the weekly join is dropping rows")
