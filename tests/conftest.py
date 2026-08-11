"""Shared fixtures.

Integration tests run against a throwaway database on the local Postgres so they
exercise real DDL rather than a mock. They are skipped automatically when no
server is reachable, which keeps the unit suite runnable anywhere.
"""

from __future__ import annotations

import os
import uuid

import pytest

from market_screener.config import PostgresSettings, load_settings
from market_screener.db.connection import Database
from market_screener.db import migrate as mig


@pytest.fixture(scope="session")
def base_pg() -> PostgresSettings:
    return load_settings().pg


@pytest.fixture(scope="session")
def pg_available(base_pg: PostgresSettings) -> bool:
    return Database(base_pg).server_version() is not None


@pytest.fixture()
def temp_db(base_pg: PostgresSettings, pg_available: bool):
    """A migrated, disposable database. Dropped on teardown."""
    if not pg_available:
        pytest.skip("no local Postgres reachable")

    name = f"ms_test_{uuid.uuid4().hex[:10]}"
    admin = Database(base_pg)
    admin.create_database(name)

    settings = PostgresSettings(
        host=base_pg.host, port=base_pg.port, database=name,
        user=base_pg.user, password=base_pg.password)
    db = Database(settings)
    mig.upgrade(db)
    try:
        yield db
    finally:
        with admin.connect(autocommit=True, database="postgres") as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="session")
def project_root():
    return load_settings().project_root
