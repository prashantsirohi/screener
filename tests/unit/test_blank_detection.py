"""
Blank-shell detection, validated against the real 2,086-company cache.

The 307 figure is not arbitrary: it is what the legacy pipeline actually hit,
and the migration plan asserts it as an exit condition. If this count moves, the
detector changed behaviour and the legacy import will not reproduce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_screener.sources.screener_parse import (blank_reason_is_retryable,
                                                    is_blank_payload, to_num)

CACHE = Path("data/fundamentals")


# ---------------- number parsing ----------------

@pytest.mark.parametrize("raw,expected", [
    ("1,39,880", 139880.0),      # Indian lakh/crore grouping
    ("48.8", 48.8),
    ("₹ 9,285", 9285.0),
    ("-12.5", -12.5),
    ("62%", 62.0),
    ("1,234.56", 1234.56),
    ("", None),
    ("-", None),
    ("NA", None),
    (None, None),
])
def test_to_num(raw, expected):
    assert to_num(raw) == expected


# ---------------- detector logic ----------------

def test_shell_with_labels_but_no_values_is_blank():
    rec = {
        "top_ratios": {"Market Cap": None, "Current Price": None, "ROCE": None},
        "profit_loss": {"Sales": {"Mar 2025": None, "Mar 2026": None},
                        "Net Profit": {"Mar 2025": None, "Mar 2026": None}},
        "balance_sheet": {}, "cash_flow": {}, "ratios": {}, "quarters": {},
    }
    blank, reason = is_blank_payload(rec)
    assert blank and reason == "numeric_spans_empty"
    assert blank_reason_is_retryable(reason)


def test_page_with_no_sections_is_blank_but_not_retryable():
    blank, reason = is_blank_payload({"top_ratios": {}, "profit_loss": {}})
    assert blank and reason == "no_tables"
    assert not blank_reason_is_retryable(reason), \
        "screener has no page for this symbol; retrying just burns request budget"


def test_a_single_value_anywhere_makes_it_not_blank():
    rec = {
        "top_ratios": {"Market Cap": None, "ROCE": None},
        "profit_loss": {"Sales": {"Mar 2026": 1234.0}},
        "balance_sheet": {}, "cash_flow": {}, "ratios": {}, "quarters": {},
    }
    assert is_blank_payload(rec) == (False, None)


def test_top_ratios_only_is_enough():
    rec = {"top_ratios": {"Market Cap": 5000.0}, "profit_loss": {}}
    assert is_blank_payload(rec) == (False, None)


def test_fetch_error_is_blank():
    blank, reason = is_blank_payload({"symbol": "X", "error": "GATED"})
    assert blank and reason == "fetch_error"


# ---------------- against the real cache ----------------

@pytest.mark.skipif(not CACHE.exists(), reason="legacy cache not present")
def test_real_cache_blank_count_is_307():
    blanks, reasons = [], {}
    total = 0
    for p in CACHE.glob("*.json"):
        total += 1
        rec = json.loads(p.read_text(encoding="utf-8"))
        blank, reason = is_blank_payload(rec)
        if blank:
            blanks.append(p.stem)
            reasons[reason] = reasons.get(reason, 0) + 1

    assert total == 2086, f"expected 2,086 cached companies, found {total}"
    assert len(blanks) == 307, (
        f"expected 307 blank shells, found {len(blanks)} (by reason: {reasons})")


@pytest.mark.skipif(not CACHE.exists(), reason="legacy cache not present")
def test_known_blank_and_known_good_symbols():
    def blank_of(sym: str):
        return is_blank_payload(
            json.loads((CACHE / f"{sym}.json").read_text(encoding="utf-8")))

    # Abbott India is a ~INR 58,000 cr company that the throttle silently
    # excluded from the legacy screen - the motivating case for the retry queue.
    assert blank_of("ABBOTINDIA")[0] is True
    for good in ("POLYCAB", "KPRMILL", "RELIANCE", "360ONE"):
        assert blank_of(good) == (False, None), f"{good} should parse cleanly"
