"""
Reporting-period label parsing.

Both non-standard forms below were found by the EAV round-trip test, not by
inspection: a Mar/Jun/Sep/Dec-only regex was dropping them, costing 1,116 facts
across the 2,086-company cache.
"""

from __future__ import annotations

from datetime import date

import pytest

from market_screener.domain.periods import (base_type, format_period_label,
                                            is_standard_length, parse_period_label)


@pytest.mark.parametrize("label,base,expected", [
    ("Mar 2026", "annual", ("annual", date(2026, 3, 31))),
    ("Jun 2025", "quarter", ("quarter", date(2025, 6, 30))),
    ("Sep 2024", "quarter", ("quarter", date(2024, 9, 30))),
    ("Dec 2023", "annual", ("annual", date(2023, 12, 31))),
    # Shareholding is sometimes filed at a non-quarter month end.
    ("Jul 2026", "quarter", ("quarter", date(2026, 7, 31))),
    ("Feb 2024", "quarter", ("quarter", date(2024, 2, 29))),   # leap year
    ("Feb 2023", "quarter", ("quarter", date(2023, 2, 28))),
    # A fiscal-year change produces one long transition period (ACC's is 15m).
    ("Mar 2023 15m", "annual", ("annual_15m", date(2023, 3, 31))),
    ("Dec 2021 9m", "annual", ("annual_9m", date(2021, 12, 31))),
])
def test_parse(label, base, expected):
    assert parse_period_label(label, base) == expected


@pytest.mark.parametrize("label", ["TTM", "", "not a period", "Mar", "2026", "Xxx 2026"])
def test_unparseable_labels_return_none(label):
    assert parse_period_label(label, "annual") is None


@pytest.mark.parametrize("label,base", [
    ("Mar 2026", "annual"),
    ("Jul 2026", "quarter"),
    ("Mar 2023 15m", "annual"),
    ("Dec 2021 9m", "annual"),
])
def test_round_trip_reproduces_the_source_header(label, base):
    period_type, report_date = parse_period_label(label, base)
    assert format_period_label(report_date, period_type) == label


def test_ttm_formats_back_to_ttm():
    assert format_period_label(date(2026, 6, 30), "ttm") == "TTM"


def test_transition_periods_are_flagged_as_non_standard():
    """A 15-month year must not be fed into a CAGR as if it were 12 months."""
    assert is_standard_length("annual")
    assert is_standard_length("quarter")
    assert not is_standard_length("annual_15m")
    assert not is_standard_length("annual_9m")


def test_base_type_strips_the_duration():
    assert base_type("annual_15m") == "annual"
    assert base_type("annual") == "annual"
    assert base_type("quarter") == "quarter"
