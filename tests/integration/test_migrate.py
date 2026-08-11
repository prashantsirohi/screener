"""Migration runner behaviour: idempotency, drift detection, ledger integrity."""

from __future__ import annotations

import pytest

from market_screener.db import migrate as mig

pytestmark = pytest.mark.integration


def test_upgrade_is_idempotent(temp_db):
    assert mig.upgrade(temp_db) == [], "a freshly migrated db should have nothing pending"
    assert mig.verify(temp_db) == []


def test_head_matches_last_migration(temp_db):
    expected = max(m.version for m in mig.discover())
    assert mig.head(temp_db) == expected


def test_all_migrations_recorded_with_checksums(temp_db):
    applied = mig.applied(temp_db)
    on_disk = {m.version: m for m in mig.discover()}
    assert set(applied) == set(on_disk)
    for version, row in applied.items():
        assert row["checksum"] == on_disk[version].checksum


def test_edited_migration_is_refused(temp_db, monkeypatch):
    """A migration changed after it was applied must halt the runner, not be
    silently layered over."""
    real = mig.discover()
    tampered = [
        mig.Migration(version=m.version, name=m.name, path=m.path,
                      sql_text=m.sql_text + "\n-- edited after apply\n")
        if m.version == "0003" else m
        for m in real
    ]
    monkeypatch.setattr(mig, "discover", lambda directory=None: tampered)

    with pytest.raises(RuntimeError, match="modified after it was applied"):
        mig.upgrade(temp_db)


def test_verify_reports_drift(temp_db, monkeypatch):
    real = mig.discover()
    tampered = [
        mig.Migration(version=m.version, name=m.name, path=m.path,
                      sql_text=m.sql_text + "\n-- drift\n")
        if m.version == "0005" else m
        for m in real
    ]
    monkeypatch.setattr(mig, "discover", lambda directory=None: tampered)

    problems = mig.verify(temp_db)
    assert any("0005" in p and "changed after it was applied" in p for p in problems)


def test_expected_tables_exist(temp_db):
    rows = temp_db.fetch_all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'market'")
    names = {r["table_name"] for r in rows}
    for expected in ("security", "price_daily", "weekly_bar", "screener_fact",
                     "screener_page_raw", "announcement", "sync_watermark",
                     "fetch_retry_queue", "screen_run", "phase1_universe",
                     "phase1_candidate", "schema_version"):
        assert expected in names, f"missing table market.{expected}"


def test_available_at_is_in_the_fundamentals_primary_key(temp_db):
    """The point-in-time guarantee is structural; assert it rather than trust it."""
    rows = temp_db.fetch_all("""
        SELECT a.attname AS col
        FROM   pg_index i
        JOIN   pg_attribute a ON a.attrelid = i.indrelid
                             AND a.attnum = ANY(i.indkey)
        WHERE  i.indrelid = 'market.screener_fact'::regclass AND i.indisprimary
    """)
    assert "available_at" in {r["col"] for r in rows}
