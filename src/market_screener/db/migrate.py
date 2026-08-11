"""
Migration runner: numbered .sql files + a schema_version ledger.

Deliberately not Alembic. Migrations are plain SQL applied in filename order,
each recorded with its SHA-256 so an edited-after-the-fact migration is caught
rather than silently diverging from what the database actually contains.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .connection import Database

log = logging.getLogger(__name__)

MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS market;
CREATE TABLE IF NOT EXISTS market.schema_version (
    version      text PRIMARY KEY,
    name         text NOT NULL,
    checksum     text NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    duration_ms  integer
);
"""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql_text: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql_text.encode("utf-8")).hexdigest()


def _migrations_dir() -> Path:
    return Path(str(resources.files("market_screener.db") / "migrations"))


def discover(directory: Path | None = None) -> list[Migration]:
    d = directory or _migrations_dir()
    if not d.exists():
        return []
    out: list[Migration] = []
    for p in sorted(d.glob("*.sql")):
        m = MIGRATION_RE.match(p.name)
        if not m:
            log.warning("skipping non-conforming migration filename: %s", p.name)
            continue
        out.append(Migration(version=m.group(1), name=m.group(2), path=p,
                             sql_text=p.read_text(encoding="utf-8")))
    return out


def applied(db: Database) -> dict[str, dict]:
    db.execute(LEDGER_DDL)
    rows = db.fetch_all(
        "SELECT version, name, checksum, applied_at FROM market.schema_version")
    return {r["version"]: r for r in rows}


def verify(db: Database, directory: Path | None = None) -> list[str]:
    """Return human-readable problems; empty list means the ledger is clean."""
    problems: list[str] = []
    have = applied(db)
    known = {m.version: m for m in discover(directory)}

    for version, row in sorted(have.items()):
        mig = known.get(version)
        if mig is None:
            problems.append(f"{version}: applied to the database but no file on disk")
        elif mig.checksum != row["checksum"]:
            problems.append(
                f"{version}_{mig.name}: file changed after it was applied "
                f"(disk {mig.checksum[:12]} != ledger {row['checksum'][:12]})")

    pending = [v for v in known if v not in have]
    for v in sorted(pending):
        problems.append(f"{v}_{known[v].name}: pending")
    return problems


def head(db: Database) -> str | None:
    have = applied(db)
    return max(have) if have else None


def upgrade(db: Database, directory: Path | None = None,
            target: str | None = None) -> list[str]:
    """Apply pending migrations in order. Returns the versions applied."""
    if not db.database_exists():
        db.create_database()

    with db.advisory_lock() as got:
        if not got:
            raise RuntimeError("another migration run holds the advisory lock")

        have = applied(db)
        pending = [m for m in discover(directory) if m.version not in have]
        if target:
            pending = [m for m in pending if m.version <= target]

        # A migration edited after being applied means the DB and the repo
        # disagree; refuse rather than layering more changes on an unknown base.
        for m in discover(directory):
            row = have.get(m.version)
            if row and row["checksum"] != m.checksum:
                raise RuntimeError(
                    f"migration {m.version}_{m.name} was modified after it was applied; "
                    f"resolve manually (disk {m.checksum[:12]} != ledger {row['checksum'][:12]})")

        done: list[str] = []
        for m in pending:
            log.info("applying migration %s_%s", m.version, m.name)
            with db.transaction() as conn, conn.cursor() as cur:
                cur.execute("SELECT clock_timestamp() AS t0")
                t0 = cur.fetchone()["t0"]
                cur.execute(m.sql_text)
                cur.execute("SELECT clock_timestamp() AS t1")
                t1 = cur.fetchone()["t1"]
                cur.execute(
                    "INSERT INTO market.schema_version "
                    "(version, name, checksum, duration_ms) VALUES (%s, %s, %s, %s)",
                    (m.version, m.name, m.checksum,
                     int((t1 - t0).total_seconds() * 1000)))
            done.append(f"{m.version}_{m.name}")
        return done
