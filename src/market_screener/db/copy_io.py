"""
Bulk load helpers.

Row-by-row INSERT is unusable at this scale - the fundamentals import alone is
~1.1M fact rows. The pattern throughout is: COPY into an UNLOGGED staging table,
then a single `INSERT ... SELECT ... ON CONFLICT` into the durable table. That
keeps the conflict policy in SQL where it can be reviewed, and keeps the load
inside one transaction.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)


def create_staging(cur: psycopg.Cursor, name: str, columns: dict[str, str]) -> None:
    """Fresh UNLOGGED staging table. Dropped and recreated so a failed prior run
    cannot contribute rows to this one."""
    cols = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(t))
        for c, t in columns.items())
    cur.execute(sql.SQL("DROP TABLE IF EXISTS staging.{}").format(sql.Identifier(name)))
    cur.execute(sql.SQL("CREATE UNLOGGED TABLE staging.{} ({})")
                .format(sql.Identifier(name), cols))


def copy_rows(cur: psycopg.Cursor, table: str, columns: Sequence[str],
              rows: Iterable[Sequence[Any]], *, schema: str = "staging") -> int:
    """COPY an iterable of tuples in. Returns the row count."""
    stmt = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        sql.Identifier(schema), sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns))
    n = 0
    with cur.copy(stmt) as cp:
        for row in rows:
            cp.write_row(row)
            n += 1
    return n


def drop_staging(cur: psycopg.Cursor, *names: str) -> None:
    for n in names:
        cur.execute(sql.SQL("DROP TABLE IF EXISTS staging.{}").format(sql.Identifier(n)))
