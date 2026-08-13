"""
The hard score floor on candidate selection.

Selection is: everything at or above the floor, capped at the target. The point
is that a weak market yields FEWER than the target rather than padding the queue
to hit a number - so the candidate count carries information instead of being a
constant 150.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from market_screener.config import ScreenSettings


def select(scores, floor, target_hi):
    """The selection rule from s85, isolated for testing."""
    el = pd.DataFrame({"preliminary_priority_score": scores}).sort_values(
        "preliminary_priority_score", ascending=False)
    pool = el[el["preliminary_priority_score"] >= floor]
    if len(pool) > target_hi:
        return pool.head(target_hi), "target"
    return pool, "floor"


def test_floor_binds_when_the_market_is_thin():
    cand, bound = select([70, 68, 65, 59, 40, 20], floor=60, target_hi=150)
    assert bound == "floor"
    assert len(cand) == 3


def test_target_caps_when_the_market_is_rich():
    cand, bound = select(list(range(60, 100)) * 10, floor=60, target_hi=150)
    assert bound == "target"
    assert len(cand) == 150


def test_the_floor_is_inclusive():
    cand, _ = select([60.0, 59.999], floor=60, target_hi=150)
    assert len(cand) == 1


def test_every_selected_name_clears_the_floor():
    cand, _ = select([95, 80, 61, 60, 59, 10], floor=60, target_hi=3)
    assert (cand["preliminary_priority_score"] >= 60).all()


def test_an_empty_pool_selects_nobody_rather_than_falling_back():
    """
    A floor nobody clears must yield an empty list. Silently backing off to the
    top-N would defeat the whole purpose of a hard floor.
    """
    cand, bound = select([50, 40, 30], floor=60, target_hi=150)
    assert bound == "floor" and len(cand) == 0


def test_exactly_at_the_target_is_bound_by_the_floor_not_the_target():
    """
    len(pool) == target_hi means the floor determined the set; the cap never
    engaged. The distinction is what the summary reports, so it must be right at
    the boundary.
    """
    cand, bound = select(list(range(70, 80)), floor=60, target_hi=10)
    assert len(cand) == 10 and bound == "floor"


# ---- configuration ----------------------------------------------------------

def test_the_shipped_floor_is_the_documented_value():
    assert ScreenSettings().min_select_score == 60.0


def test_floor_is_in_the_config_hash():
    """Two runs on different floors must be distinguishable in the run table."""
    from pathlib import Path

    from market_screener.config import HttpSettings, PostgresSettings, Settings

    def mk(floor):
        return Settings(
            project_root=Path("."), domain="operational", paths=None,
            pg=PostgresSettings(), http=HttpSettings(),
            screen=dataclasses.replace(ScreenSettings(), min_select_score=floor))

    assert mk(60.0).config_hash() != mk(68.0).config_hash()


@pytest.mark.parametrize("raw", ["abc", "-5", "101"])
def test_an_invalid_floor_env_value_is_rejected(monkeypatch, raw):
    """A bad floor must fail loudly rather than silently reverting to a default."""
    from market_screener.config import _screen_settings

    monkeypatch.setenv("SCREENER_MIN_SELECT_SCORE", raw)
    with pytest.raises(ValueError, match="SCREENER_MIN_SELECT_SCORE"):
        _screen_settings()


def test_the_floor_env_override_is_read(monkeypatch):
    from market_screener.config import _screen_settings

    monkeypatch.setenv("SCREENER_MIN_SELECT_SCORE", "68")
    assert _screen_settings().min_select_score == 68.0


def test_s85_reads_the_floor_from_settings_not_a_module_constant():
    """
    These were hardcoded module constants while ScreenSettings carried unused
    copies that still fed config_hash - so changing the config produced a new
    hash and an identical candidate set. Guard against the regression.
    """
    import inspect

    from market_screener.pipeline.stages import s85_phase1_outputs as s85

    src = inspect.getsource(s85.run)
    assert "ctx.settings.screen.min_select_score" in src
    assert "ctx.settings.screen.candidate_target_high" in src
