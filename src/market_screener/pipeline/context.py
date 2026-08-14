"""
Run and stage contracts.

Modelled on the ai-trading-system convention: a stage receives a RunContext,
returns a StageResult, and registers its artifacts with a SHA-256 so a run can be
reconstructed and diffed later.

One rule is load-bearing: `as_of` lives on the context and is threaded into every
query. No stage may call now() or current_date, because a run must produce the
same answer whenever it is replayed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db.connection import Database

IST = timezone(timedelta(hours=5, minutes=30))


def pit_cutoff(as_of: date) -> datetime:
    """
    Everything knowable by the close of `as_of`, in market time.

    A single instant, derived once and threaded into every point-in-time read,
    so no stage can invent its own boundary. Exclusive: rows are admitted with
    `< cutoff`, i.e. strictly before midnight IST that ends the as_of day.
    """
    return datetime.combine(as_of + timedelta(days=1),
                            datetime.min.time(), tzinfo=IST)


def new_run_id(phase: int, as_of: date) -> str:
    return f"p{phase}-{as_of.isoformat()}-{uuid.uuid4().hex[:8]}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


@dataclass
class StageArtifact:
    name: str
    kind: str            # csv | md | json | parquet
    path: Path
    row_count: int | None = None

    @property
    def sha256(self) -> str:
        return sha256_file(self.path)

    @property
    def bytes(self) -> int:
        return self.path.stat().st_size


@dataclass
class StageResult:
    stage: str
    status: str = "complete"          # complete | skipped | failed
    rows_in: int | None = None
    rows_out: int | None = None
    skip_reason: str | None = None
    error: str | None = None
    artifacts: list[StageArtifact] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunContext:
    run_id: str
    phase: int
    as_of: date
    settings: Settings
    db: Database
    params: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)   # passed between stages

    @property
    def pit_cutoff(self) -> datetime:
        """The point-in-time boundary for every fact this run may read."""
        return pit_cutoff(self.as_of)

    @property
    def run_dir(self) -> Path:
        return self.settings.paths.run_dir(self.run_id)

    def stage_dir(self, stage: str, attempt: int = 1) -> Path:
        d = self.settings.paths.stage_dir(self.run_id, stage, attempt)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def output_dir(self) -> Path:
        d = self.run_dir / "output"
        d.mkdir(parents=True, exist_ok=True)
        return d


def compute_input_hash(parts: dict[str, Any]) -> str:
    """
    Fingerprint a stage's inputs.

    An identical hash on a later run means nothing the stage reads has changed,
    so it can be skipped. Counts and max-timestamps are enough: they move
    whenever a sync writes anything the stage would consume.
    """
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def data_fingerprint(db: Database, as_of: date,
                     settings: Settings | None = None) -> dict[str, Any]:
    """
    The state of every table the screen reads, condensed to a few numbers -
    plus the configuration and code version that decide what it does with them.

    Data alone is not enough. Six distinct config hashes once shared a single
    input hash for every stage, which made gated and ungated runs, and both
    price bases, mutually interchangeable to the cache: a run would reuse
    another configuration's output while recording its own config_hash. Only
    the habit of passing --force kept the results honest.

    `metric_model` covers the other blind spot. A formula change in domain/
    moves no row count and no timestamp, so without a version to bump the cache
    would serve results computed by code that no longer exists.
    """
    row = db.fetch_one("""
        SELECT
          (SELECT count(*) FROM market.security WHERE is_active)          AS securities,
          (SELECT count(*) FROM market.weekly_bar WHERE is_complete)      AS weekly_bars,
          (SELECT max(week_end_date) FROM market.weekly_bar
            WHERE is_complete)                                            AS last_week,
          (SELECT count(*) FROM market.screener_fact)                     AS facts,
          (SELECT max(available_at) FROM market.screener_fact)            AS last_fact_at,
          (SELECT count(*) FROM market.corporate_action)                  AS actions,
          (SELECT count(*) FROM market.announcement_classification
            WHERE taxonomy_version LIKE 'v1:%%')                          AS event_flags
    """)
    from ..domain.metrics import MODEL_VERSION

    out = {"as_of": as_of.isoformat(),
           **{k: str(v) for k, v in (row or {}).items()},
           "metric_model": MODEL_VERSION}
    if settings is not None:
        out["config_hash"] = settings.config_hash()
    return out
