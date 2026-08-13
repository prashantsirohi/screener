"""
Which price steps count as a missing corporate action.

This is the discriminator behind the reconciliation alert, and getting it wrong
makes the alert useless in one of two ways. Too loose and it flags every
dividend payer - the first version reported 304 securities when the real figure
was 87. Too tight and a genuinely missing split goes unnoticed.
"""

from __future__ import annotations

import pytest

from market_screener.ingest.price_derive import _ACTION_RATIOS, _is_action_ratio


# ---------------- ratios real actions produce ----------------

@pytest.mark.parametrize("step,what", [
    (0.5, "1:2 split, or a 1:1 bonus"),
    (0.2, "1:5 split, or face value 10 -> 2"),
    (0.1, "1:10 split, face value 10 -> 1"),
    (0.3333, "2:1 bonus"),
    (0.6667, "1:2 bonus"),
    (0.75, "1:3 bonus"),
    (0.25, "3:1 bonus"),
    (0.1667, "1:5 bonus"),
    (0.8, "1:4 bonus"),
    (2.0, "2:1 consolidation"),
    (5.0, "5:1 consolidation"),
    (10.0, "10:1 consolidation"),
])
def test_real_action_ratios_are_recognised(step, what):
    assert _is_action_ratio(step), f"{step} ({what}) should count"


def test_nykaa_style_bonus_is_recognised():
    """NYKAA's 1:5 bonus shows as a step of 0.167 - an 83.3% move."""
    assert _is_action_ratio(0.167)


def test_small_measurement_error_still_snaps():
    """An observed step is never exact; 3% either side still counts."""
    for step in (0.49, 0.51, 0.196, 0.204, 9.8, 10.2):
        assert _is_action_ratio(step), step


# ---------------- what must NOT count ----------------

@pytest.mark.parametrize("step,why", [
    (1.0, "no move at all"),
    (1.02, "ordinary drift"),
    (1.08, "below the minimum move"),
    (0.93, "below the minimum move"),
])
def test_small_moves_are_not_actions(step, why):
    assert not _is_action_ratio(step), f"{step} ({why}) must not count"


@pytest.mark.parametrize("step", [1.15, 1.17, 1.22, 1.31, 1.44, 0.87, 0.72, 0.58])
def test_dividend_scale_divergence_is_not_an_action(step):
    """
    The failure this guards against.

    A special dividend or rights issue moves Yahoo's total-return series and not
    the price-return one, by an untidy amount. Fraction.limit_denominator(20)
    finds a "clean" fraction for all of these - 1.17 becomes 7/6 - which is why
    the first implementation classified 217 dividend payers as missing splits.
    """
    assert not _is_action_ratio(step), f"{step} is dividend-scale divergence"


@pytest.mark.parametrize("step", [0.0, -1.0, None])
def test_degenerate_inputs_are_rejected(step):
    assert not _is_action_ratio(step)


# ---------------- the ratio table itself ----------------

def test_common_splits_have_a_matching_consolidation():
    """1:2, 1:5 and 1:10 splits all have a real reverse counterpart."""
    for split, consolidation in ((0.5, 2.0), (0.2, 5.0), (0.1, 10.0)):
        assert _is_action_ratio(split) and _is_action_ratio(consolidation)


def test_bonus_ratios_are_not_blindly_inverted():
    """
    A 1:2 bonus gives 0.667, but 1.5 is not a corporate action - no consolidation
    raises a price by half. Listing those inverses blankets the 1.1-1.5 band
    where dividend divergence lives.
    """
    for not_an_action in (1.2, 1.25, 1.3333, 1.5):
        assert not _is_action_ratio(not_an_action), \
            f"{not_an_action} is the inverse of a bonus, not a real action"


def test_no_ratio_sits_inside_the_dead_band():
    """A ratio within 10% of 1.0 could never be distinguished from noise."""
    for r in _ACTION_RATIOS:
        assert abs(r - 1.0) >= 0.10, f"{r} is too close to 1.0 to be usable"


def test_the_table_is_not_dense_enough_to_match_anything():
    """
    Sanity on the discriminator's selectivity: a spread of arbitrary values must
    mostly NOT match, or the test above is vacuous.
    """
    arbitrary = [1.0 + i * 0.017 for i in range(6, 40)]
    matched = sum(1 for v in arbitrary if _is_action_ratio(v))
    assert matched / len(arbitrary) < 0.4, (
        f"{matched}/{len(arbitrary)} arbitrary values matched - the table is "
        f"too dense to discriminate")
