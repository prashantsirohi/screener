"""
Postgres access. Postgres is the single writer and the source of record.

All SQL is parameterised. Never build SQL by string interpolation of scraped
values - that is the one pattern from market_intel we explicitly do not carry
over. Where an identifier must be dynamic, use psycopg.sql composition.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ..config import PostgresSettings

log = logging.getLogger(__name__)

# Guards concurrent `screener migrate` invocations.
MIGRATION_LOCK_KEY = 0x5C_1EE_11


class Database:
    def __init__(self, pg: PostgresSettings):
        self.pg = pg

    # ---------- connections ----------
    @contextmanager
    def connect(self, *, autocommit: bool = False,
                database: str | None = None) -> Iterator[psycopg.Connection]:
        conn = psycopg.connect(self.pg.conninfo(database), row_factory=dict_row,
                               autocommit=autocommit)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Commit on clean exit, roll back on any exception."""
        with self.connect() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ---------- convenience ----------
    def fetch_all(self, query: str, params: Sequence[Any] | dict | None = None) -> list[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def fetch_one(self, query: str, params: Sequence[Any] | dict | None = None) -> dict | None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()

    def fetch_value(self, query: str, params: Sequence[Any] | dict | None = None) -> Any:
        row = self.fetch_one(query, params)
        return next(iter(row.values())) if row else None

    def execute(self, query: str, params: Sequence[Any] | dict | None = None) -> int:
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount

    def execute_returning(self, query: str,
                          params: Sequence[Any] | dict | None = None) -> list[dict]:
        """
        Run a writing statement with RETURNING and COMMIT it.

        `fetch_all` opens a non-autocommit connection and never commits, so an
        `UPDATE ... RETURNING` through it hands back rows whose write is then
        rolled back on close. For a queue claim that is silent corruption: the
        caller believes it holds the row, nothing is marked in_flight, and a
        concurrent run can claim the same item.
        """
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else []

    # ---------- bootstrap ----------
    def server_version(self) -> str | None:
        try:
            with self.connect(autocommit=True, database="postgres") as conn, conn.cursor() as cur:
                cur.execute("SELECT version() AS v")
                row = cur.fetchone()
                return row["v"] if row else None
        except psycopg.Error as exc:
            log.debug("server_version failed: %s", exc)
            return None

    def database_exists(self, name: str | None = None) -> bool:
        name = name or self.pg.database
        with self.connect(autocommit=True, database="postgres") as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            return cur.fetchone() is not None

    def create_database(self, name: str | None = None) -> bool:
        """CREATE DATABASE cannot run inside a transaction; needs autocommit."""
        name = name or self.pg.database
        if self.database_exists(name):
            return False
        with self.connect(autocommit=True, database="postgres") as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'")
                        .format(sql.Identifier(name)))
        log.info("created database %s", name)
        return True

    def ping(self) -> bool:
        try:
            return self.fetch_value("SELECT 1 AS ok") == 1
        except psycopg.Error:
            return False

    @contextmanager
    def advisory_lock(self, key: int = MIGRATION_LOCK_KEY) -> Iterator[bool]:
        """Session-scoped advisory lock so two migrate runs cannot interleave."""
        with self.connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (key,))
            row = cur.fetchone()
            got = bool(row and row["got"])
            try:
                yield got
            finally:
                if got:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
