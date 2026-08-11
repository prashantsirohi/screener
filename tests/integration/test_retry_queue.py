"""
Retry-queue mechanics.

The queue lives in Postgres specifically so a crash cannot lose work. That is
only true if an abandoned claim comes back: `claim()` flips rows to in_flight,
and without a lease timeout a killed process strands them there permanently.
"""

from __future__ import annotations

import pytest

from market_screener.ingest.fundamentals_sync import (SOURCE, claim,
                                                      reclaim_stale_claims,
                                                      release)

pytestmark = pytest.mark.integration


def _seed(db, scope: str, *, state="pending", attempts=0, due="now()"):
    db.execute(f"""
        INSERT INTO market.fetch_retry_queue
            (source, scope, reason, attempts, state, next_attempt_at)
        VALUES (%s, %s, 'blank_page', %s, %s, {due})
        ON CONFLICT (source, scope) DO UPDATE SET
            state = EXCLUDED.state, attempts = EXCLUDED.attempts,
            next_attempt_at = EXCLUDED.next_attempt_at
    """, (SOURCE, scope, attempts, state))


def test_claim_is_exclusive(temp_db):
    _seed(temp_db, "AAA")
    first = claim(temp_db, 10)
    second = claim(temp_db, 10)
    assert [r["symbol"] for r in first] == ["AAA"]
    assert second == [], "a claimed row must not be handed out twice"


def test_only_due_rows_are_claimed(temp_db):
    _seed(temp_db, "DUE", due="now() - interval '1 minute'")
    _seed(temp_db, "NOTDUE", due="now() + interval '2 hours'")
    got = {r["symbol"] for r in claim(temp_db, 10)}
    assert got == {"DUE"}


def test_abandoned_claim_returns_to_pending(temp_db):
    """The crash-recovery guarantee."""
    _seed(temp_db, "CRASHED")
    claimed = claim(temp_db, 10)
    assert len(claimed) == 1

    # Simulate the holder dying: the row stays in_flight with an old claim.
    temp_db.execute(
        "UPDATE market.fetch_retry_queue SET claimed_at = now() - interval '2 hours' "
        "WHERE scope = 'CRASHED'")

    assert reclaim_stale_claims(temp_db, minutes=30) == 1
    state = temp_db.fetch_value(
        "SELECT state FROM market.fetch_retry_queue WHERE scope = 'CRASHED'")
    assert state == "pending"
    assert [r["symbol"] for r in claim(temp_db, 10)] == ["CRASHED"]


def test_a_fresh_claim_is_not_reclaimed(temp_db):
    _seed(temp_db, "WORKING")
    claim(temp_db, 10)
    assert reclaim_stale_claims(temp_db, minutes=30) == 0
    assert temp_db.fetch_value(
        "SELECT state FROM market.fetch_retry_queue WHERE scope = 'WORKING'") == "in_flight"


def test_release_resolved_marks_done(temp_db):
    _seed(temp_db, "OK")
    r = claim(temp_db, 1)[0]
    release(temp_db, r["retry_id"], resolved=True, attempts=r["attempts"])
    assert temp_db.fetch_value(
        "SELECT state FROM market.fetch_retry_queue WHERE scope = 'OK'") == "resolved"


def test_release_failed_escalates_the_backoff(temp_db):
    _seed(temp_db, "FAIL")
    r = claim(temp_db, 1)[0]
    release(temp_db, r["retry_id"], resolved=False, attempts=r["attempts"], error="blank")
    row = temp_db.fetch_one(
        "SELECT state, attempts, next_attempt_at > now() AS deferred "
        "FROM market.fetch_retry_queue WHERE scope = 'FAIL'")
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["deferred"] is True


def test_attempts_eventually_exhaust(temp_db):
    _seed(temp_db, "GIVEUP", attempts=5)
    r = claim(temp_db, 1)[0]
    release(temp_db, r["retry_id"], resolved=False, attempts=r["attempts"], error="blank")
    assert temp_db.fetch_value(
        "SELECT state FROM market.fetch_retry_queue WHERE scope = 'GIVEUP'") == "exhausted"


def test_exhausted_rows_are_never_claimed_again(temp_db):
    _seed(temp_db, "DEAD", state="exhausted")
    assert claim(temp_db, 10) == []
