"""
Settings resolution: environment > .env > built-in defaults.

Secrets only ever come from the environment or .env. Non-secret tuning lives in
code here so it is reviewable in diffs.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from .paths import DataDomain, DataDomainPaths, canonicalize_project_root, get_domain_paths

AnalyticsMode = Literal["attach", "parquet"]

# Which return basis the technical layer runs on, for the WHOLE run.
#
# Mixing is not an option: Yahoo's adjclose is total return and the bhavcopy
# series is price return, so splicing them steps the series by the cumulative
# dividend yield, and relative strength divides a stock by a benchmark that must
# be on the same basis to mean anything.
#
# The default stays yahoo_adjclose because the benchmark indices only exist on
# that basis - they are not in bhavcopy. Moving to split_bonus is the deliberate
# cutover step, and needs price-return index series first.
PriceBasis = Literal["yahoo_adjclose", "split_bonus"]


def _env(key: str, default: str | None = None) -> str | None:
    v = os.getenv(key)
    return v if v not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class PostgresSettings:
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "market_screener"
    user: str = "postgres"
    password: str | None = None
    connect_timeout: int = 15

    def conninfo(self, database: str | None = None) -> str:
        """libpq conninfo string. Password is omitted when unset (trust auth)."""
        parts = [
            f"host={self.host}",
            f"port={self.port}",
            f"dbname={database or self.database}",
            f"user={self.user}",
            f"connect_timeout={self.connect_timeout}",
        ]
        if self.password:
            parts.append(f"password={self.password}")
        return " ".join(parts)

    def duckdb_attach_string(self) -> str:
        parts = [f"host={self.host}", f"port={self.port}",
                 f"dbname={self.database}", f"user={self.user}"]
        if self.password:
            parts.append(f"password={self.password}")
        return " ".join(parts)

    def redacted(self) -> dict:
        d = asdict(self)
        d["password"] = "***" if self.password else None
        return d


@dataclass(frozen=True)
class HttpSettings:
    """Defaults tuned for NSE/BSE, which lock out below ~1.5s between requests."""
    min_request_gap_sec: float = 1.5
    jitter_low: float = 0.1
    jitter_high: float = 0.4
    max_attempts: int = 3
    adapter_retries: int = 3
    backoff_factor: float = 1.5
    timeout_sec: int = 40
    user_agent: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


@dataclass(frozen=True)
class ScreenSettings:
    mcap_min_inr_cr: float = 1000.0
    mcap_max_inr_cr: float = 100000.0
    liquidity_min_inr_cr: float = 1.0
    min_annual_periods: int = 3
    min_weekly_bars: int = 40
    candidate_target_low: int = 100
    candidate_target_high: int = 150
    min_select_score: float = 45.0
    # screener.in refresh cadence; the staleness gate is what keeps steady-state
    # load at ~25 pages/day instead of 2,086.
    fundamentals_max_age_days: int = 45


@dataclass(frozen=True)
class Settings:
    project_root: Path
    domain: DataDomain
    paths: DataDomainPaths
    pg: PostgresSettings
    http: HttpSettings
    screen: ScreenSettings
    analytics_mode: AnalyticsMode = "attach"
    price_basis: PriceBasis = "yahoo_adjclose"
    screener_session_cookie: str | None = field(default=None, repr=False)
    log_level: str = "INFO"

    def config_hash(self) -> str:
        """Stable hash of screening-relevant config, recorded on every run."""
        payload = {
            "screen": asdict(self.screen),
            "analytics_mode": self.analytics_mode,
            "price_basis": self.price_basis,
            "domain": self.domain,
        }
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def summary(self) -> dict:
        return {
            "project_root": str(self.project_root),
            "domain": self.domain,
            "data_root": str(self.paths.data_root),
            "reports_root": str(self.paths.reports_root),
            "logs_root": str(self.paths.logs_root),
            "postgres": self.pg.redacted(),
            "analytics_mode": self.analytics_mode,
            "session_cookie_set": bool(self.screener_session_cookie),
            "config_hash": self.config_hash(),
        }


def load_settings(domain: DataDomain = "operational",
                  project_root: Path | str | None = None) -> Settings:
    root = canonicalize_project_root(project_root)
    load_dotenv(root / ".env", override=False)

    mode = (_env("SCREENER_ANALYTICS_MODE", "attach") or "attach").lower()
    if mode not in ("attach", "parquet"):
        mode = "attach"

    basis = (_env("SCREENER_PRICE_BASIS", "yahoo_adjclose") or "yahoo_adjclose").lower()
    if basis not in ("yahoo_adjclose", "split_bonus"):
        basis = "yahoo_adjclose"

    return Settings(
        project_root=root,
        domain=domain,
        paths=get_domain_paths(domain, root),
        pg=PostgresSettings(
            host=_env("SCREENER_PG_HOST", "127.0.0.1"),
            port=_env_int("SCREENER_PG_PORT", 5432),
            database=_env("SCREENER_PG_DATABASE", "market_screener"),
            user=_env("SCREENER_PG_USER", "postgres"),
            password=_env("SCREENER_PG_PASSWORD"),
        ),
        http=HttpSettings(),
        screen=ScreenSettings(),
        analytics_mode=mode,  # type: ignore[arg-type]
        price_basis=basis,    # type: ignore[arg-type]
        screener_session_cookie=_env("SCREENER_SESSION_COOKIE"),
        log_level=_env("SCREENER_LOG_LEVEL", "INFO") or "INFO",
    )
