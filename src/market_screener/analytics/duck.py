"""
DuckDB analytics session.

Postgres owns the data; DuckDB only reads it. Two modes:

* **attach** - `ATTACH ... TYPE postgres (READ_ONLY)`, the default. Needs the
  `postgres` extension, a one-time download from extensions.duckdb.org.
* **parquet** - Postgres tables are exported to Parquet first and DuckDB reads
  the files. A real fallback for a host that cannot fetch the extension.

Both modes expose the same `src_*` views, so every query in analytics/sql is
mode-agnostic and switching is a config change rather than a rewrite.

Two-engine contract enforced here:
  1. The attachment is READ_ONLY. DuckDB can write to Postgres; we forbid it, so
     there is exactly one writer.
  3. `as_of` is always a bound parameter. No now()/current_date in any query.
  4. Numerics are cast to DOUBLE once, in the src_ views, so DuckDB and pandas
     agree on float64 and rounding cannot diverge.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

from ..config import Settings

log = logging.getLogger(__name__)

# Tables the analytics layer is allowed to read.
SOURCE_TABLES = ("security", "price_daily", "corporate_action", "price_daily_adj",
                 "weekly_bar", "weekly_bar_resolved", "weekly_bar_source_choice",
                 "price_source_reconciliation", "trading_calendar",
                 "screener_fact", "metric_dim", "index_membership")

# The single cast point. Everything downstream sees DOUBLE, never NUMERIC.
SRC_VIEWS = {
    "src_security": """
        SELECT security_id, isin, symbol, exchange, series, security_type,
               company_name, CAST(face_value AS DOUBLE) AS face_value,
               listing_date, nse_industry, is_active
        FROM   {src}.security
    """,
    "src_price_daily": """
        SELECT security_id, trade_date,
               CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high,
               CAST(low AS DOUBLE) AS low, CAST(close AS DOUBLE) AS close,
               CAST(prev_close AS DOUBLE) AS prev_close,
               CAST(volume AS BIGINT) AS volume,
               CAST(turnover_inr AS DOUBLE) AS turnover_inr,
               source
        FROM   {src}.price_daily
    """,
    "src_corporate_action": """
        SELECT security_id, ex_date, action_type,
               CAST(ratio_from AS DOUBLE) AS ratio_from,
               CAST(ratio_to AS DOUBLE) AS ratio_to,
               CAST(amount_inr AS DOUBLE) AS amount_inr,
               CAST(adjustment_factor AS DOUBLE) AS adjustment_factor,
               source, confidence
        FROM   {src}.corporate_action
    """,
    # Every source's bars, including partial weeks. Reconciliation only.
    "src_weekly_bar_all": """
        SELECT security_id, week_end_date, iso_year, iso_week,
               CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high,
               CAST(low AS DOUBLE) AS low, CAST(close AS DOUBLE) AS close,
               CAST(volume AS BIGINT) AS volume,
               CAST(turnover_inr AS DOUBLE) AS turnover_inr,
               day_count, source, source_rank, adj_basis,
               is_complete, last_trade_date
        FROM   {src}.weekly_bar
        WHERE  is_complete
    """,
    # The resolved series everything analytical should read: one source and one
    # return basis per security, complete weeks only. A partial week would
    # otherwise pull the latest MA point and the 52-week high toward a bar
    # holding one or two sessions.
    "src_weekly_bar": """
        SELECT security_id, week_end_date, iso_year, iso_week,
               CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high,
               CAST(low AS DOUBLE) AS low, CAST(close AS DOUBLE) AS close,
               CAST(volume AS BIGINT) AS volume,
               CAST(turnover_inr AS DOUBLE) AS turnover_inr,
               day_count, source, source_rank, adj_basis,
               is_complete, last_trade_date
        FROM   {src}.weekly_bar_resolved
        WHERE  is_complete
    """,
    "src_price_reconciliation": """
        SELECT security_id, as_of_date, weeks_compared, weeks_matching,
               CAST(max_step_pct AS DOUBLE) AS max_step_pct,
               CAST(median_diff_pct AS DOUBLE) AS median_diff_pct,
               verdict
        FROM   {src}.price_source_reconciliation r
        WHERE  as_of_date = (SELECT max(as_of_date)
                             FROM {src}.price_source_reconciliation)
    """,
    "src_weekly_source_choice": """
        SELECT security_id, source, source_rank, adj_basis, bars,
               first_week, last_week
        FROM   {src}.weekly_bar_source_choice
    """,
    "src_price_daily_adj": """
        SELECT security_id, trade_date,
               CAST(adj_open AS DOUBLE) AS adj_open,
               CAST(adj_high AS DOUBLE) AS adj_high,
               CAST(adj_low AS DOUBLE) AS adj_low,
               CAST(adj_close AS DOUBLE) AS adj_close,
               CAST(adj_volume AS BIGINT) AS adj_volume,
               CAST(cum_adj_factor AS DOUBLE) AS cum_adj_factor,
               adj_basis
        FROM   {src}.price_daily_adj
    """,
}


class AnalyticsUnavailable(RuntimeError):
    """DuckDB cannot reach the data in the configured mode."""


def _attach_postgres(con: duckdb.DuckDBPyConnection, settings: Settings) -> str:
    try:
        con.execute("INSTALL postgres;")
        con.execute("LOAD postgres;")
    except duckdb.Error as exc:
        raise AnalyticsUnavailable(
            f"DuckDB postgres extension unavailable ({exc}). "
            f"Set SCREENER_ANALYTICS_MODE=parquet to use the Parquet fallback."
        ) from exc

    conninfo = settings.pg.duckdb_attach_string()
    con.execute(f"ATTACH '{conninfo}' AS pg (TYPE postgres, READ_ONLY);")
    return "pg.market"


def _attach_parquet(con: duckdb.DuckDBPyConnection, settings: Settings) -> str:
    root = settings.paths.parquet_dir
    missing = [t for t in SOURCE_TABLES if not (root / f"{t}.parquet").exists()]
    if missing:
        raise AnalyticsUnavailable(
            f"parquet mode needs exports for {missing}; run `screener export-parquet`")
    con.execute("CREATE SCHEMA IF NOT EXISTS pq;")
    for t in SOURCE_TABLES:
        p = (root / f"{t}.parquet").as_posix()
        con.execute(f"CREATE OR REPLACE VIEW pq.{t} AS SELECT * FROM read_parquet('{p}')")
    return "pq"


@contextmanager
def analytics_session(settings: Settings,
                      memory_limit: str | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open DuckDB with the src_ views defined for the configured mode."""
    con = duckdb.connect(database=":memory:")
    try:
        if memory_limit:
            con.execute(f"SET memory_limit='{memory_limit}'")
        con.execute("SET TimeZone='Asia/Kolkata'")

        src = (_attach_postgres(con, settings) if settings.analytics_mode == "attach"
               else _attach_parquet(con, settings))
        for name, body in SRC_VIEWS.items():
            con.execute(f"CREATE OR REPLACE VIEW {name} AS {body.format(src=src)}")
        log.debug("analytics session ready (mode=%s, src=%s)", settings.analytics_mode, src)
        yield con
    finally:
        con.close()


def load_sql(name: str) -> str:
    """Read a query from analytics/sql, kept as files so they are reviewable."""
    p = Path(__file__).parent / "sql" / name
    if not p.exists():
        raise FileNotFoundError(p)
    return p.read_text(encoding="utf-8")


def export_parquet(settings: Settings, db_execute) -> dict[str, int]:
    """Materialise the source tables to Parquet for the offline analytics mode."""
    out: dict[str, int] = {}
    root = settings.paths.parquet_dir
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{settings.pg.duckdb_attach_string()}' AS pg "
                    f"(TYPE postgres, READ_ONLY);")
        for t in SOURCE_TABLES:
            target = (root / f"{t}.parquet").as_posix()
            con.execute(f"COPY (SELECT * FROM pg.market.{t}) TO '{target}' (FORMAT PARQUET)")
            out[t] = con.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]
    finally:
        con.close()
    return out
