"""
Which dates the price sync considers.

The walker must probe EVERY calendar day, not just Monday-Friday. NSE runs
occasional weekend sessions - disaster-recovery tests and Diwali Muhurat - and
a published bhavcopy was confirmed for 2024-01-20 (Sat), 2023-11-12 (Sun),
2024-03-02 (Sat) and 2024-05-18 (Sat).

Missing one of those sessions is not a cosmetic gap: the weekly bar for that week
loses a day, and the prev-close corporate-action inference reads the resulting
discontinuity as a split. The first run of this pipeline manufactured 46 phantom
"consolidations" on 2024-01-23 for exactly this reason.
"""

from __future__ import annotations

from datetime import date, timedelta


def candidate_dates(start: date, until: date, calendar: dict[date, bool],
                    have: set[date]) -> list[date]:
    """Mirror of the selection logic in price_sync.sync."""
    out: list[date] = []
    d = start
    while d <= until:
        if calendar.get(d) is False:
            pass
        elif d in have:
            pass
        else:
            out.append(d)
        d += timedelta(days=1)
    return out


KNOWN_WEEKEND_SESSIONS = [
    date(2024, 1, 20),
    date(2023, 11, 12),
    date(2024, 3, 2),
    date(2024, 5, 18),
]


def test_weekend_sessions_are_candidates():
    for d in KNOWN_WEEKEND_SESSIONS:
        got = candidate_dates(d, d, calendar={}, have=set())
        assert got == [d], (
            f"{d} ({d.strftime('%a')}) is a confirmed NSE trading session and must "
            f"be probed; a weekday-only walker silently drops it")


def test_known_non_trading_days_are_skipped():
    d = date(2026, 8, 15)
    assert candidate_dates(d, d, calendar={d: False}, have=set()) == []


def test_already_loaded_dates_are_skipped():
    d = date(2026, 8, 10)
    assert candidate_dates(d, d, calendar={}, have={d}) == []


def test_calendar_caches_the_weekend_probe():
    """Once probed and found empty, a weekend is never fetched again."""
    start, until = date(2026, 8, 1), date(2026, 8, 9)
    first = candidate_dates(start, until, calendar={}, have=set())
    assert len(first) == 9, "a fresh range probes every day"

    learned = {d: (d.weekday() < 5) for d in first}
    second = candidate_dates(start, until, calendar=learned, have=set(first))
    assert second == [], "a second pass repeats nothing"


def test_full_range_includes_both_weekend_days():
    got = candidate_dates(date(2026, 8, 7), date(2026, 8, 10), {}, set())
    assert [d.strftime("%a") for d in got] == ["Fri", "Sat", "Sun", "Mon"]
