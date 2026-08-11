"""
Shared helper for parity tests: align the oracle's bar set with the port's.

The store deliberately excludes the current partial week - a Monday cutoff would
otherwise emit a bar stamped the coming Friday holding one or two sessions, and
that bar drags the latest moving-average point and the 52-week high.

The frozen oracle has no such notion and happily analyses the partial week. So a
straight comparison measures the partial-week policy, not the port. Truncating
the oracle's frame to the same complete weeks isolates what parity is actually
for: did moving the data path from JSON files to Postgres change any number.

The policy itself is tested separately, in
tests/integration/test_price_pipeline.py::test_partial_weeks_are_flagged_not_dated_into_the_future.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd


def iso_friday(d) -> "pd.Timestamp":
    ts = pd.Timestamp(d).normalize()
    return ts + timedelta(days=4 - ts.weekday())


def truncate_to_complete_weeks(legacy_frame: pd.DataFrame,
                               last_complete_week) -> pd.DataFrame:
    """Drop oracle bars belonging to a week the store treats as incomplete."""
    if legacy_frame is None or legacy_frame.empty:
        return legacy_frame
    cutoff = pd.Timestamp(last_complete_week).normalize()
    keep = legacy_frame["date"].map(iso_friday) <= cutoff
    return legacy_frame.loc[keep].reset_index(drop=True)


def last_complete_week(ported_frame: pd.DataFrame):
    """The port's final week_end_date - by construction a completed week."""
    return pd.Timestamp(ported_frame["date"].max()).normalize()
