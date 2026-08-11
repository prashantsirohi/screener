"""
Filesystem layout.

Follows the ai-trading-system convention: a project root plus three overridable
roots (data / reports / logs), and an operational-vs-research domain split so a
research run can never write over production artifacts.

Rule inherited from that repo: application code never hardcodes a repo-relative
`data/...` path. It asks for a DataDomainPaths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DataDomain = Literal["operational", "research"]

_ENV_ROOTS = {
    "data": "DATA_ROOT",
    "reports": "REPORTS_ROOT",
    "logs": "LOGS_ROOT",
}


def canonicalize_project_root(start: Path | str | None = None) -> Path:
    """Walk up from `start` until a pyproject.toml is found; else use cwd."""
    p = Path(start) if start else Path(__file__).resolve()
    if p.is_file():
        p = p.parent
    for candidate in [p, *p.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate.resolve()
    return Path.cwd().resolve()


@dataclass(frozen=True)
class DataDomainPaths:
    project_root: Path
    domain: DataDomain
    data_root: Path
    reports_root: Path
    logs_root: Path

    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw"

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "cache"

    @property
    def parquet_dir(self) -> Path:
        return self.data_root / "parquet"

    @property
    def duckdb_path(self) -> Path:
        """Scratch DuckDB file. Analytics are ephemeral; Postgres is the record."""
        return self.data_root / f"analytics_{self.domain}.duckdb"

    def run_dir(self, run_id: str) -> Path:
        return self.reports_root / "runs" / run_id

    def stage_dir(self, run_id: str, stage: str, attempt: int = 1) -> Path:
        return self.run_dir(run_id) / stage / f"attempt_{attempt}"

    def ensure(self) -> "DataDomainPaths":
        for d in (self.data_root, self.reports_root, self.logs_root,
                  self.raw_dir, self.cache_dir, self.parquet_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


def get_domain_paths(domain: DataDomain = "operational",
                     project_root: Path | str | None = None) -> DataDomainPaths:
    root = canonicalize_project_root(project_root)
    suffix = "" if domain == "operational" else f"_{domain}"

    def resolve(kind: str) -> Path:
        env = os.getenv(_ENV_ROOTS[kind])
        base = Path(env) if env else root / kind
        return (base if domain == "operational" else base.parent / f"{base.name}{suffix}").resolve()

    return DataDomainPaths(
        project_root=root,
        domain=domain,
        data_root=resolve("data"),
        reports_root=resolve("reports"),
        logs_root=resolve("logs"),
    )
