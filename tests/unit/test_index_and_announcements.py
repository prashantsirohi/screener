"""Unit coverage for the index-close and announcement collectors."""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from market_screener.ingest.announcement_sync import _hash, _norm, _parse_dt
from market_screener.sources.nse_indices import INDEX_SYMBOLS, _norm as idx_norm

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------- index name mapping ----------------

@pytest.mark.parametrize("raw,expected", [
    ("Nifty 50", "NIFTY_50"),
    ("NIFTY 50", "NIFTY_50"),
    ("  Nifty   500  ", "NIFTY_500"),
    ("Nifty Bank", "NIFTY_BANK"),
    ("Nifty PSU Bank", "NIFTY_PSUBANK"),
    ("Nifty Infrastructure", "NIFTY_INFRA"),
])
def test_index_names_map_to_store_symbols(raw, expected):
    assert INDEX_SYMBOLS[idx_norm(raw)] == expected


def test_unmapped_indices_are_ignored():
    """The file carries ~163 indices; only the tracked benchmarks are kept."""
    assert idx_norm("Nifty Smallcap 250") not in INDEX_SYMBOLS


def test_every_mapped_symbol_is_distinct():
    vals = list(INDEX_SYMBOLS.values())
    assert len(vals) == len(set(vals))


# ---------------- announcement hashing ----------------

def test_hash_is_stable_for_identical_input():
    when = datetime(2026, 8, 10, 15, 4, tzinfo=IST)
    a = _hash("RELIANCE", "Board Meeting Outcome", when, "x.pdf", "123")
    b = _hash("RELIANCE", "Board  Meeting   Outcome", when, "x.pdf", "123")
    assert a == b, "whitespace and case must not change the identity of a filing"


def test_hash_changes_with_any_identifying_field():
    when = datetime(2026, 8, 10, 15, 4, tzinfo=IST)
    base = _hash("RELIANCE", "Outcome", when, "x.pdf", "123")
    assert base != _hash("TCS", "Outcome", when, "x.pdf", "123")
    assert base != _hash("RELIANCE", "Different", when, "x.pdf", "123")
    assert base != _hash("RELIANCE", "Outcome", when + timedelta(hours=1), "x.pdf", "123")
    assert base != _hash("RELIANCE", "Outcome", when, "y.pdf", "123")
    assert base != _hash("RELIANCE", "Outcome", when, "x.pdf", "456")


def test_overlapping_windows_produce_the_same_hash():
    """Windows overlap by a day on purpose; the hash makes the re-read free."""
    when = datetime(2026, 8, 9, 11, 0, tzinfo=IST)
    assert (_hash("INFY", "Scheme of Arrangement", when, None, "9") ==
            _hash("INFY", "Scheme of Arrangement", when, None, "9"))


@pytest.mark.parametrize("raw,expected", [
    ("10-Aug-2026 15:04:33", datetime(2026, 8, 10, 15, 4, 33, tzinfo=IST)),
    ("10-Aug-2026", datetime(2026, 8, 10, 0, 0, tzinfo=IST)),
    ("2026-08-10 15:04:33", datetime(2026, 8, 10, 15, 4, 33, tzinfo=IST)),
])
def test_announcement_timestamps_parse(raw, expected):
    assert _parse_dt(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "not a date", "31-Feb-2026"])
def test_unparseable_timestamps_return_none(raw):
    assert _parse_dt(raw) is None


def test_norm_collapses_whitespace_and_case():
    assert _norm("  Board   MEETING\noutcome ") == "board meeting outcome"
