"""
Parsing NSE's free-text corporate-action subject into an adjustment factor.

Getting a factor backwards silently rescales a security's entire price history,
so each direction is asserted explicitly rather than just the magnitude.
"""

from __future__ import annotations

import pytest

from market_screener.sources.nse_corporate_actions import parse_subject


@pytest.mark.parametrize("subject,kind,factor", [
    # Face-value splits: FV 10 -> 1 means the price divides by ten.
    ("Face Value Split (Sub-Division) - From Rs 10/- To Rs 1/-", "split", 0.1),
    ("Face Value Split (Sub-Division) - From Rs 10/- To Rs 2/-", "split", 0.2),
    ("Face Value Split From Rs. 5/- To Rs. 1/-", "split", 0.2),
    ("FACE VALUE SPLIT (SUB-DIVISION) - FROM RS 2/- TO RE 1/-", "split", 0.5),
])
def test_splits(subject, kind, factor):
    got = parse_subject(subject)
    assert got is not None, subject
    assert got[0] == kind
    assert got[1] == pytest.approx(factor, abs=1e-6)


@pytest.mark.parametrize("subject,factor", [
    # a:b = a new shares for every b held, so b shares become a+b.
    ("Bonus 1:1", 0.5),          # doubles the count
    ("Bonus 1:2", 2 / 3),        # 2 -> 3
    ("Bonus 2:1", 1 / 3),        # 1 -> 3
    ("Bonus 3:5", 5 / 8),
    ("Bonus issue 1:10", 10 / 11),
])
def test_bonuses(subject, factor):
    got = parse_subject(subject)
    assert got is not None, subject
    assert got[0] == "bonus"
    assert got[1] == pytest.approx(factor, abs=1e-6)


def test_consolidation_raises_the_price():
    got = parse_subject("Consolidation of shares From Rs 1/- To Rs 10/-")
    assert got is not None
    assert got[0] == "consolidation"
    assert got[1] == pytest.approx(10.0)


@pytest.mark.parametrize("subject", [
    "Dividend - Rs 12 Per Share",
    "Interim Dividend Rs 5.50",
    "Annual General Meeting",
    "Rights Issue 1:4",          # needs a subscription price we do not have
    "",
    "Scheme of Arrangement",
])
def test_non_price_actions_are_ignored(subject):
    assert parse_subject(subject) is None


def test_a_split_never_returns_a_factor_above_one():
    """Direction guard: a split must reduce the pre-ex price, never raise it."""
    for s in ("Face Value Split (Sub-Division) - From Rs 10/- To Rs 1/-",
              "Face Value Split From Rs 100/- To Rs 5/-"):
        kind, factor, *_ = parse_subject(s)
        assert kind == "split" and factor < 1.0


def test_bonus_factor_is_always_between_zero_and_one():
    for s in ("Bonus 1:1", "Bonus 5:1", "Bonus 1:20"):
        _, factor, *_ = parse_subject(s)
        assert 0.0 < factor < 1.0
