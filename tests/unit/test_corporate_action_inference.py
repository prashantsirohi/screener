"""
Corporate-action inference, on synthetic price series.

Runs the real SQL against a hand-built src_price_daily so the guards can be
exercised deterministically, without Postgres and without a network.

Note the semantics these assert. An earlier version of the query assumed NSE
restates PRVSCLSGPRIC on an ex-date; measured against three years of bhavcopy it
does not - the ratio was exactly 1.000 across all 269 confirmed gaps. The signal
is close-to-close, and these tests pin that down so the assumption cannot quietly
flip back.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from market_screener.analytics.duck import load_sql

SQL = load_sql("infer_corporate_actions.sql")

DEFAULTS = {"as_of": date(2026, 1, 31), "min_move": 0.28,
            "max_gap_days": 5, "snap_tol": 0.05}


def run(rows, **overrides):
    """rows: (security_id, trade_date, close, volume)"""
    params = {**DEFAULTS, **overrides}
    con = duckdb.connect()
    con.execute("CREATE TABLE t (security_id BIGINT, trade_date DATE, "
                "close DOUBLE, prev_close DOUBLE, volume BIGINT)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?)",
                    [(sid, d, c, c, v) for sid, d, c, v in rows])
    con.execute("CREATE VIEW src_price_daily AS SELECT * FROM t")
    out = con.execute(SQL, params).df()
    con.close()
    return out


def walk(sid, start, closes, step_days=1, volume=1000):
    return [(sid, start + timedelta(days=i * step_days), c, volume)
            for i, c in enumerate(closes)]


# ---------------- detection ----------------

def test_quiet_series_yields_nothing():
    assert run(walk(1, date(2026, 1, 1), [100, 101, 100.5, 102, 101.5])).empty


def test_one_for_two_split_is_detected():
    out = run(walk(1, date(2026, 1, 1), [200, 201, 100.5]))
    assert len(out) == 1
    r = out.iloc[0]
    assert r["ex_date"].date() == date(2026, 1, 3)
    assert r["snap_ratio"] == pytest.approx(0.5, abs=1e-6)
    assert r["action_type"] == "split_or_bonus"


def test_one_for_ten_split_is_detected():
    out = run(walk(1, date(2026, 1, 1), [1000, 1010, 101]))
    assert len(out) == 1
    assert out.iloc[0]["snap_ratio"] == pytest.approx(0.1, abs=1e-6)


def test_a_real_split_with_a_same_day_move_still_snaps():
    """SIGACHI's 1:10 showed an observed factor of 0.0966, not 0.1000."""
    out = run(walk(1, date(2026, 1, 1), [408.90, 408.90, 39.50]))
    assert len(out) == 1
    assert out.iloc[0]["snap_ratio"] == pytest.approx(0.1, abs=1e-6)
    assert out.iloc[0]["factor"] == pytest.approx(0.0966, abs=1e-3)


def test_consolidation_is_detected():
    out = run(walk(1, date(2026, 1, 1), [10, 10.1, 101]))
    assert len(out) == 1
    assert out.iloc[0]["action_type"] == "consolidation"
    assert out.iloc[0]["snap_ratio"] == pytest.approx(10.0, abs=1e-6)


# ---------------- discrimination ----------------

def test_ordinary_moves_are_below_the_threshold():
    """Circuit limits cap a session at 20%; nothing there is a corporate action."""
    assert run(walk(1, date(2026, 1, 1), [100, 100, 82])).empty


def test_a_crash_that_lands_on_no_clean_ratio_is_rejected():
    """A 37% fall is not near 0.5, 0.6, 0.6667 or 0.75, so it is not an action."""
    assert run(walk(1, date(2026, 1, 1), [100, 100, 63])).empty


def test_snap_tolerance_bounds_what_counts_as_a_ratio():
    # 0.52 is within 5% of 0.5 -> accepted
    assert len(run(walk(1, date(2026, 1, 1), [100, 100, 52]))) == 1
    # 0.56 is 12% off 0.5 and 6.7% off 0.6 -> rejected
    assert run(walk(1, date(2026, 1, 1), [100, 100, 56])).empty


# ---------------- guards ----------------

def test_gap_across_missing_history_is_ignored():
    """The regression that produced 1,761 phantom events."""
    rows = [(1, date(2023, 12, 14), 100.0, 1000),
            (1, date(2026, 7, 30), 410.0, 1000)]
    assert run(rows, as_of=date(2026, 8, 11)).empty


def test_weekend_gap_within_tolerance_still_detects():
    rows = [(1, date(2026, 1, 2), 200.0, 1000),    # Friday
            (1, date(2026, 1, 5), 100.0, 1000)]    # Monday, ex-date
    out = run(rows)
    assert len(out) == 1
    assert out.iloc[0]["snap_ratio"] == pytest.approx(0.5, abs=1e-6)


def test_gap_just_beyond_tolerance_is_rejected():
    rows = [(1, date(2026, 1, 2), 200.0, 1000),
            (1, date(2026, 1, 9), 100.0, 1000)]    # 7 days > max_gap_days=5
    assert run(rows).empty


def test_each_security_is_evaluated_independently():
    rows = walk(1, date(2026, 1, 1), [100, 101, 102]) + \
           walk(2, date(2026, 1, 1), [200, 201, 100.5])
    out = run(rows)
    assert list(out["security_id"]) == [2]
