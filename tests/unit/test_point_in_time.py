"""
Point-in-time correctness and stage-cache integrity.

Two failure modes, both of which produced confident wrong answers rather than
errors:

* a screen dated in the past reading facts and announcements that arrived after
  it - 172,435 facts and 552 announcements, measured;
* the stage cache treating six different configurations as interchangeable,
  because the fingerprint covered data but not config.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_screener.config import (HttpSettings, PostgresSettings,
                                    ScreenSettings, Settings)
from market_screener.pipeline.context import IST, compute_input_hash, pit_cutoff

# ---- the cutoff -------------------------------------------------------------


def test_cutoff_is_midnight_ist_after_the_as_of_day():
    c = pit_cutoff(date(2026, 8, 13))
    assert c == datetime(2026, 8, 14, 0, 0, tzinfo=IST)


def test_cutoff_admits_the_whole_as_of_day():
    """23:59 IST on the as_of day is in; the next instant is out."""
    c = pit_cutoff(date(2026, 8, 13))
    assert datetime(2026, 8, 13, 23, 59, 59, tzinfo=IST) < c
    assert not datetime(2026, 8, 14, 0, 0, 0, tzinfo=IST) < c


def test_cutoff_is_timezone_aware():
    """A naive datetime compared against timestamptz raises in Postgres."""
    assert pit_cutoff(date(2026, 8, 13)).tzinfo is not None


def test_cutoff_is_ist_not_utc():
    """
    An 18:30 UTC boundary would admit a whole extra evening of Indian market
    time - or exclude one - depending on which side of it a fact landed.
    """
    c = pit_cutoff(date(2026, 8, 13))
    assert c.utcoffset() == timedelta(hours=5, minutes=30)
    assert c.astimezone(timezone.utc) == datetime(
        2026, 8, 13, 18, 30, tzinfo=timezone.utc)


def test_cutoff_moves_with_as_of():
    assert pit_cutoff(date(2026, 8, 10)) < pit_cutoff(date(2026, 8, 13))


# ---- the readers accept a bound ---------------------------------------------

def test_event_flags_accepts_an_as_of():
    """It had no parameter at all, so the screen could not bound it."""
    from market_screener.ingest.classify_events import event_flags

    assert "as_of" in inspect.signature(event_flags).parameters


def test_event_flags_bounds_on_announced_at():
    """
    Market time, not available_at. Backfilled rows all share one scrape date,
    so bounding on available_at would make every backdated run see nothing.
    """
    from market_screener.ingest import classify_events

    src = inspect.getsource(classify_events.event_flags)
    assert "a.announced_at <" in src


def test_provenance_readers_accept_a_cutoff():
    from market_screener.domain import provenance

    for fn in (provenance.global_sources, provenance.company_sources):
        assert "cutoff" in inspect.signature(fn).parameters


def test_the_screen_bounds_both_fundamentals_and_events():
    """
    The regression itself: both reads existed and both were unbounded, while
    the underlying functions already supported a cutoff.

    Fundamentals are now bulk-loaded, so the bound moved from the per-security
    call to `payloads_for_universe`. The assertion tracks the boundary, not the
    call shape - what must hold is that the screen never reads fundamentals or
    events without a cutoff, however they are fetched.
    """
    from market_screener.pipeline.stages import s80_phase1_screen as s80

    src = inspect.getsource(s80.run)
    assert "ctx.pit_cutoff" in src
    assert "payloads_for_universe(db, as_of=ctx.pit_cutoff" in src
    assert 'event_flags(db, "v1", as_of=cutoff)' in src
    assert "payload_for_metrics(db, sid" not in src, \
        "the per-security N+1 read is back in the universe loop"


# ---- the stage fingerprint --------------------------------------------------

def mk_settings(**screen_over) -> Settings:
    return Settings(
        project_root=Path("."), domain="operational", paths=None,
        pg=PostgresSettings(), http=HttpSettings(),
        screen=dataclasses.replace(ScreenSettings(), **screen_over))


def fingerprint_for(settings: Settings) -> dict:
    """data_fingerprint's config half, without touching a database."""
    from market_screener.domain.metrics import MODEL_VERSION

    return {"as_of": "2026-08-13", "facts": "1151022",
            "metric_model": MODEL_VERSION,
            "config_hash": settings.config_hash()}


@pytest.mark.parametrize("field,value", [
    ("min_select_score", 68.0),
    ("technical_gate_exclude_stages", ()),
    ("technical_gate_min_rs_13w", 0.0),
    ("mcap_min_inr_cr", 2000.0),
    ("liquidity_min_inr_cr", 2.0),
    ("candidate_target_high", 100),
    ("min_weekly_bars", 52),
])
def test_every_screen_setting_changes_the_input_hash(field, value):
    """
    Any knob that changes what the screen decides must invalidate the cache.
    Before this, changing the score floor or the technical gate left the hash
    untouched and a non-forced run served the previous configuration's output.
    """
    base = compute_input_hash({"stage": "s80", **fingerprint_for(mk_settings())})
    other = compute_input_hash(
        {"stage": "s80", **fingerprint_for(mk_settings(**{field: value}))})
    assert base != other, f"{field} does not affect the stage input hash"


def test_identical_config_reproduces_the_hash():
    """The cache must still work; invalidating everything is not the fix."""
    a = compute_input_hash({"stage": "s80", **fingerprint_for(mk_settings())})
    b = compute_input_hash({"stage": "s80", **fingerprint_for(mk_settings())})
    assert a == b


def test_metric_model_version_is_in_the_fingerprint():
    """
    A formula change moves no row count and no timestamp, so without a version
    the cache would serve results computed by code that no longer exists.
    """
    fp = fingerprint_for(mk_settings())
    base = compute_input_hash({"stage": "s80", **fp})
    bumped = compute_input_hash({"stage": "s80", **{**fp, "metric_model": "2"}})
    assert base != bumped


def test_reuse_lookup_is_filtered_by_config_hash():
    """
    Second, independent barrier. If the fingerprint ever misses a knob again,
    the query itself still refuses to cross a configuration boundary.
    """
    from market_screener.pipeline import orchestrator

    src = inspect.getsource(orchestrator._previous_matching_run)
    assert "r.config_hash = %s" in src
    assert "config_hash" in inspect.signature(
        orchestrator._previous_matching_run).parameters
