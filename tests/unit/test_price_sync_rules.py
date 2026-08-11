"""
Publication-lag rule.

A weekday with no bhavcopy is only a holiday once the file would certainly have
been published. Before that it just has not landed yet - and writing it off as a
holiday would exclude a real trading session from every subsequent sync, because
the calendar is consulted before the fetch.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from market_screener.ingest.price_sync import IST, _is_settled

TODAY = date(2026, 8, 11)


def at(h: int, m: int = 0, d: date = TODAY) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def test_past_dates_are_always_settled():
    assert _is_settled(date(2026, 8, 10), now=at(10, 0))
    assert _is_settled(date(2020, 1, 2), now=at(10, 0))


def test_future_dates_are_never_settled():
    assert not _is_settled(date(2026, 8, 12), now=at(23, 59))


@pytest.mark.parametrize("hour,expected", [
    (9, False),    # market open
    (15, False),   # just before close
    (16, False),   # closed, bhavcopy not out
    (18, False),   # still within the publication window
    (19, True),    # published by now
    (23, True),
])
def test_today_settles_only_after_the_publication_window(hour, expected):
    assert _is_settled(TODAY, now=at(hour)) is expected


def test_the_regression_this_guards():
    """At 10:24 IST the first run marked today a non-trading day."""
    assert not _is_settled(TODAY, now=at(10, 24)), (
        "a missing bhavcopy during market hours must not be recorded as a holiday")
