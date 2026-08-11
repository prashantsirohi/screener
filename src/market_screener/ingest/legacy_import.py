"""
One-time import of the JSON/CSV caches the legacy pipeline left in data/.

Reads C:\\Trading\\data in place and never mutates it. Idempotent: every step
is an ON CONFLICT upsert or DO NOTHING insert, so re-running changes nothing.

This is what makes the migration cheap - 2,086 screener pages, 2,098 price
series and 233k announcements are already on disk, so the new store starts full
and the retry queue only has to chase the 307 blanks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from ..domain import metric_map as mm
from ..domain.periods import parse_period_label
from ..sources.screener_parse import is_blank_payload, blank_reason_is_retryable
from .sync_state import (close_batch, mark_stale_batches, open_batch,
                         record_error, set_watermark)

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

STATEMENT_KEYS = ("profit_loss", "balance_sheet", "cash_flow",
                  "ratios", "quarters", "shareholding")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_period(label: str, base: str = "annual") -> date | None:
    """Report-date only. Callers needing the period type use parse_period_label."""
    got = parse_period_label(label, base)
    return got[1] if got else None


def iso_friday(d: date) -> date:
    """Friday of the ISO week containing d.

    Yahoo stamps weekly bars at week-START (Monday); a bhavcopy resample lands on
    week-END. Normalising both to the same Friday here is what stops the
    stock/benchmark relative-strength join from dropping every row.
    """
    return d + timedelta(days=4 - d.weekday())


def _sha(*parts: Any) -> str:
    blob = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _norm_text(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def symbol_id_map(db: Database) -> dict[str, int]:
    rows = db.fetch_all(
        "SELECT symbol, security_id FROM market.security WHERE exchange = 'NSE'")
    return {r["symbol"]: r["security_id"] for r in rows}


# --------------------------------------------------------------------------
# step 1 - universe
# --------------------------------------------------------------------------

def import_universe(db: Database, data_root: Path) -> dict:
    raw = data_root / "raw"
    eq_path = raw / "nse_equity_list.csv"
    if not eq_path.exists():
        raise FileNotFoundError(eq_path)

    eq = pd.read_csv(eq_path)
    eq.columns = [c.strip().upper().replace(" ", "_") for c in eq.columns]
    eq["SYMBOL"] = eq["SYMBOL"].astype(str).str.strip()
    eq["SERIES"] = eq["SERIES"].astype(str).str.strip()

    available_at = datetime.fromtimestamp(eq_path.stat().st_mtime, IST)
    bid = open_batch(db, "legacy.universe", {"file": str(eq_path)})

    # Index files carry NSE's own industry label.
    industry: dict[str, str] = {}
    members: list[tuple[str, str, str]] = []   # (index_name, symbol, industry)
    idx_files = {
        "NIFTY_TOTAL_MARKET": raw / "idx_niftytotalmarket.csv",
        "NIFTY_MICROCAP_250": raw / "idx_microcap250.csv",
        "NIFTY_500": raw / "idx_nifty500.csv",
    }
    for index_name, p in idx_files.items():
        if not p.exists():
            continue
        d = pd.read_csv(p)
        d.columns = [c.strip() for c in d.columns]
        for r in d.itertuples(index=False):
            sym = str(getattr(r, "Symbol", "")).strip()
            ind = getattr(r, "Industry", None)
            if not sym:
                continue
            if ind and sym not in industry:
                industry[sym] = str(ind).strip()
            members.append((index_name, sym, str(ind).strip() if ind else None))

    def listing_date(v) -> date | None:
        try:
            return datetime.strptime(str(v).strip(), "%d-%b-%Y").date()
        except (ValueError, TypeError):
            return None

    rows = []
    for r in eq.itertuples(index=False):
        sym = r.SYMBOL
        rows.append((
            (str(r.ISIN_NUMBER).strip() or None) if pd.notna(getattr(r, "ISIN_NUMBER", None)) else None,
            sym, "NSE", r.SERIES, "equity",
            str(r.NAME_OF_COMPANY).strip() if pd.notna(r.NAME_OF_COMPANY) else None,
            float(r.FACE_VALUE) if pd.notna(getattr(r, "FACE_VALUE", None)) else None,
            listing_date(getattr(r, "DATE_OF_LISTING", None)),
            industry.get(sym),
            r.SERIES == "EQ",
        ))

    cols = ("isin", "symbol", "exchange", "series", "security_type", "company_name",
            "face_value", "listing_date", "nse_industry", "is_active")
    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "security_in", {
            "isin": "text", "symbol": "text", "exchange": "text", "series": "text",
            "security_type": "text", "company_name": "text", "face_value": "numeric",
            "listing_date": "date", "nse_industry": "text", "is_active": "boolean"})
        copy_rows(cur, "security_in", cols, rows)
        cur.execute("""
            INSERT INTO market.security
                (isin, symbol, exchange, series, security_type, company_name,
                 face_value, listing_date, nse_industry, is_active)
            SELECT isin, symbol, exchange, series, security_type, company_name,
                   face_value, listing_date, nse_industry, is_active
            FROM   staging.security_in
            ON CONFLICT (symbol, exchange) DO UPDATE SET
                isin         = COALESCE(EXCLUDED.isin, market.security.isin),
                series       = EXCLUDED.series,
                company_name = COALESCE(EXCLUDED.company_name, market.security.company_name),
                face_value   = COALESCE(EXCLUDED.face_value, market.security.face_value),
                listing_date = COALESCE(EXCLUDED.listing_date, market.security.listing_date),
                nse_industry = COALESCE(EXCLUDED.nse_industry, market.security.nse_industry),
                is_active    = EXCLUDED.is_active,
                last_seen_at = now()
        """)
        cur.execute("""
            INSERT INTO market.security_alias (security_id, symbol, exchange, valid_from, source)
            SELECT s.security_id, s.symbol, s.exchange, COALESCE(s.listing_date, CURRENT_DATE),
                   'nse_equity_l'
            FROM   market.security s
            ON CONFLICT DO NOTHING
        """)
        # Anything not in the current EQUITY_L is no longer tradable, whatever it
        # was when a historical bhavcopy registered it. The price history stays;
        # only the screening universe narrows.
        cur.execute("""
            UPDATE market.security s
            SET    is_active = false
            WHERE  s.exchange = 'NSE' AND s.security_type = 'equity' AND s.is_active
              AND  NOT EXISTS (SELECT 1 FROM staging.security_in n
                               WHERE n.symbol = s.symbol AND n.exchange = s.exchange)
        """)
        deactivated = cur.rowcount
        drop_staging(cur, "security_in")

    smap = symbol_id_map(db)
    snapshot = available_at.date()
    mem_rows = [(snapshot, idx, smap[sym], ind, "nse_index_csv")
                for idx, sym, ind in members if sym in smap]
    if mem_rows:
        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "idx_in", {
                "snapshot_date": "date", "index_name": "text", "security_id": "bigint",
                "industry": "text", "source": "text"})
            copy_rows(cur, "idx_in",
                      ("snapshot_date", "index_name", "security_id", "industry", "source"),
                      mem_rows)
            cur.execute("""
                INSERT INTO market.index_membership
                    (snapshot_date, index_name, security_id, industry, source)
                SELECT DISTINCT ON (snapshot_date, index_name, security_id)
                       snapshot_date, index_name, security_id, industry, source
                FROM   staging.idx_in
                ON CONFLICT (snapshot_date, index_name, security_id) DO NOTHING
            """)
            drop_staging(cur, "idx_in")

    n_eq = int((eq["SERIES"] == "EQ").sum())
    active = db.fetch_value(
        "SELECT count(*) AS c FROM market.security "
        "WHERE series='EQ' AND security_type='equity' AND is_active")
    close_batch(db, bid, total=len(rows), ok=len(rows), rows=len(rows) + len(mem_rows))
    set_watermark(db, "legacy.universe", "*", snapshot.isoformat(), rows=len(rows))
    return {"securities": len(rows), "series_eq": n_eq, "active_eq": active,
            "deactivated": deactivated, "index_rows": len(mem_rows),
            "with_industry": len(industry)}


# --------------------------------------------------------------------------
# step 2 - the one cached bhavcopy day
# --------------------------------------------------------------------------

def import_bhavcopy_cache(db: Database, data_root: Path) -> dict:
    """
    Loads data/raw/nse_bhavcopy.csv.

    The legacy loader renamed TTLTRADVAL/TTLTRFVOL, neither of which exists - the
    real UDiFF headers are TTLTRADGVOL (volume) and TTLTRFVAL (turnover). So the
    legacy spine has never carried turnover at all, and the liquidity gate has
    been entirely Yahoo-derived. Fixed here, with an assertion so a future header
    change fails loudly instead of writing an all-NULL column.
    """
    p = data_root / "raw" / "nse_bhavcopy.csv"
    if not p.exists():
        return {"skipped": "no cached bhavcopy"}

    b = pd.read_csv(p)
    b.columns = [c.strip().upper() for c in b.columns]
    required = {"TCKRSYMB", "SCTYSRS", "CLSPRIC", "TTLTRADGVOL", "TTLTRFVAL", "TRADDT"}
    missing = required - set(b.columns)
    if missing:
        raise ValueError(f"bhavcopy is missing required columns: {sorted(missing)}")

    b = b[b["SCTYSRS"].astype(str).str.strip() == "EQ"].copy()
    b["TCKRSYMB"] = b["TCKRSYMB"].astype(str).str.strip()
    trade_date = pd.to_datetime(b["TRADDT"].iloc[0]).date()

    smap = symbol_id_map(db)
    bid = open_batch(db, "legacy.bhavcopy", {"file": str(p), "trade_date": str(trade_date)})

    def num(v):
        return None if pd.isna(v) else float(v)

    rows = []
    for r in b.itertuples(index=False):
        sid = smap.get(r.TCKRSYMB)
        if sid is None:
            continue
        rows.append((
            sid, trade_date,
            num(getattr(r, "OPNPRIC", None)), num(getattr(r, "HGHPRIC", None)),
            num(getattr(r, "LWPRIC", None)), num(r.CLSPRIC),
            num(getattr(r, "PRVSCLSGPRIC", None)),
            int(r.TTLTRADGVOL) if pd.notna(r.TTLTRADGVOL) else None,
            num(r.TTLTRFVAL),
            int(getattr(r, "TTLNBOFTXSEXCTD", 0)) if pd.notna(getattr(r, "TTLNBOFTXSEXCTD", None)) else None,
            "EQ", "nse_bhavcopy",
        ))

    cols = ("security_id", "trade_date", "open", "high", "low", "close", "prev_close",
            "volume", "turnover_inr", "trade_count", "series", "source")
    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "price_in", {
            "security_id": "bigint", "trade_date": "date", "open": "numeric",
            "high": "numeric", "low": "numeric", "close": "numeric",
            "prev_close": "numeric", "volume": "bigint", "turnover_inr": "numeric",
            "trade_count": "bigint", "series": "text", "source": "text"})
        copy_rows(cur, "price_in", cols, rows)
        cur.execute(f"""
            INSERT INTO market.price_daily ({", ".join(cols)})
            SELECT {", ".join(cols)} FROM staging.price_in
            ON CONFLICT (security_id, trade_date) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO market.trading_calendar (exchange, trade_date, is_trading, source)
            VALUES ('NSE', %s, true, 'nse_bhavcopy')
            ON CONFLICT (exchange, trade_date) DO NOTHING
        """, (trade_date,))
        drop_staging(cur, "price_in")

    with_turnover = db.fetch_value(
        "SELECT count(*) AS c FROM market.price_daily "
        "WHERE trade_date = %s AND turnover_inr IS NOT NULL", (trade_date,))
    close_batch(db, bid, total=len(rows), ok=len(rows), rows=len(rows))
    set_watermark(db, "prices.nse_bhavcopy", "*", trade_date.isoformat(), rows=len(rows))
    return {"trade_date": str(trade_date), "rows": len(rows),
            "with_turnover": with_turnover}


# --------------------------------------------------------------------------
# step 3 - screener fundamentals
# --------------------------------------------------------------------------

def _facts_from_payload(rec: dict, quarters_latest: date | None,
                        snapshot_date: date) -> Iterator[tuple]:
    """
    Yield (period_type, report_date, metric_id, value) for one payload.

    NULL values are stored, not skipped. "The company reported this line blank
    for this period" is different information from "this period is absent", and
    collapsing the two changes what `series[-1]` returns - which silently moved
    opm_latest_pct and inventory_days when the two paths were compared.
    """
    basis_stmts = {
        "profit_loss": "annual", "balance_sheet": "annual",
        "cash_flow": "annual", "ratios": "annual",
        "quarters": "quarter", "shareholding": "quarter",
    }
    for stmt, base_period in basis_stmts.items():
        block = rec.get(stmt) or {}
        for label, series in block.items():
            mid = mm.metric_id(stmt, label)
            for period_label, value in (series or {}).items():
                if period_label == "TTM":
                    if quarters_latest is None:
                        continue
                    yield ("ttm", quarters_latest, mid, value)
                    continue
                parsed = parse_period_label(period_label, base_period)
                if parsed is None:
                    continue
                period_type, rd = parsed
                yield (period_type, rd, mid, value)

    rd_snap = quarters_latest or snapshot_date
    for label, value in (rec.get("top_ratios") or {}).items():
        yield ("snapshot", rd_snap, mm.metric_id("top_ratios", label), value)

    # Compounded-growth block: keyed by range, not date.
    for table, ranges in (rec.get("growth") or {}).items():
        for range_label, value in (ranges or {}).items():
            yield ("range", rd_snap, mm.growth_metric_id(table, range_label), value)


def import_fundamentals(db: Database, data_root: Path) -> dict:
    cache = data_root / "fundamentals"
    files = sorted(cache.glob("*.json"))
    if not files:
        return {"skipped": "no cached fundamentals"}

    smap = symbol_id_map(db)
    bid = open_batch(db, "legacy.fundamentals", {"dir": str(cache), "files": len(files)})

    metric_rows: dict[str, dict] = {}
    page_rows: list[tuple] = []
    fact_rows: list[tuple] = []
    blanks: list[tuple[str, str]] = []
    unmapped = 0

    for p in files:
        rec = json.loads(p.read_text(encoding="utf-8"))
        sym = rec.get("symbol") or p.stem
        sid = smap.get(sym)
        if sid is None:
            unmapped += 1
            continue

        blank, reason = is_blank_payload(rec)
        scraped = rec.get("scraped_at")
        fetched_at = (pd.to_datetime(scraped, utc=True).to_pydatetime()
                      if scraped else datetime.now(timezone.utc))
        basis = rec.get("basis") or "consolidated"
        payload = json.dumps(rec, ensure_ascii=False)

        page_rows.append((
            sid, basis, rec.get("source_url") or f"https://www.screener.in/company/{sym}/",
            fetched_at, payload, _sha(payload), blank, reason, "v1"))

        if blank:
            blanks.append((sym, reason or "unknown"))
            continue

        q = rec.get("quarters") or {}
        q_periods: set[date] = set()
        for series in q.values():
            for lbl in (series or {}):
                d = parse_period(lbl)
                if d:
                    q_periods.add(d)
        q_latest = max(q_periods) if q_periods else None

        for stmt in STATEMENT_KEYS:
            for label in (rec.get(stmt) or {}):
                d = mm.describe(stmt, label)
                metric_rows.setdefault(d["metric_id"], d)
        for label in (rec.get("top_ratios") or {}):
            d = mm.describe("top_ratios", label)
            metric_rows.setdefault(d["metric_id"], d)
        for table, ranges in (rec.get("growth") or {}).items():
            for range_label in (ranges or {}):
                gid = mm.growth_metric_id(table, range_label)
                metric_rows.setdefault(gid, {
                    "metric_id": gid, "metric_label": f"{table} :: {range_label}",
                    "statement": "growth", "unit": "pct",
                    "higher_is_better": True, "description": None})

        snap_date = fetched_at.date()
        for period_type, report_date, mid, value in _facts_from_payload(
                rec, q_latest, snap_date):
            fact_rows.append((sid, period_type, report_date, basis, mid,
                              None if value is None else float(value),
                              fetched_at, "screener", bid))

    with db.transaction() as conn, conn.cursor() as cur:
        # metric_dim first: screener_fact has an FK onto it
        create_staging(cur, "metric_in", {
            "metric_id": "text", "metric_label": "text", "statement": "text",
            "unit": "text", "higher_is_better": "boolean", "description": "text"})
        copy_rows(cur, "metric_in",
                  ("metric_id", "metric_label", "statement", "unit",
                   "higher_is_better", "description"),
                  [(d["metric_id"], d["metric_label"], d["statement"], d["unit"],
                    d["higher_is_better"], d["description"]) for d in metric_rows.values()])
        cur.execute("""
            INSERT INTO market.metric_dim
                (metric_id, metric_label, statement, unit, higher_is_better, description)
            SELECT DISTINCT ON (metric_id)
                   metric_id, metric_label, statement, unit, higher_is_better, description
            FROM   staging.metric_in
            ON CONFLICT (metric_id) DO NOTHING
        """)

        create_staging(cur, "page_in", {
            "security_id": "bigint", "basis": "text", "source_url": "text",
            "fetched_at": "timestamptz", "payload": "jsonb", "payload_hash": "text",
            "is_blank": "boolean", "blank_reason": "text", "parser_version": "text"})
        copy_rows(cur, "page_in",
                  ("security_id", "basis", "source_url", "fetched_at", "payload",
                   "payload_hash", "is_blank", "blank_reason", "parser_version"),
                  page_rows)
        cur.execute("""
            INSERT INTO market.screener_page_raw
                (security_id, basis, source_url, fetched_at, payload, payload_hash,
                 is_blank, blank_reason, parser_version)
            SELECT security_id, basis, source_url, fetched_at, payload, payload_hash,
                   is_blank, blank_reason, parser_version
            FROM   staging.page_in
            ON CONFLICT (security_id, basis, fetched_at) DO NOTHING
        """)

        create_staging(cur, "fact_in", {
            "security_id": "bigint", "period_type": "text", "report_date": "date",
            "statement_basis": "text", "metric_id": "text", "value": "numeric",
            "available_at": "timestamptz", "source": "text", "sync_batch_id": "text"})
        copy_rows(cur, "fact_in",
                  ("security_id", "period_type", "report_date", "statement_basis",
                   "metric_id", "value", "available_at", "source", "sync_batch_id"),
                  fact_rows)
        cur.execute("""
            INSERT INTO market.screener_fact
                (security_id, period_type, report_date, statement_basis, metric_id,
                 value, available_at, source, page_id, sync_batch_id)
            SELECT f.security_id, f.period_type, f.report_date, f.statement_basis,
                   f.metric_id, f.value, f.available_at, f.source, pr.page_id,
                   f.sync_batch_id
            FROM   staging.fact_in f
            LEFT JOIN market.screener_page_raw pr
                   ON pr.security_id = f.security_id
                  AND pr.basis       = f.statement_basis
                  AND pr.fetched_at  = f.available_at
            ON CONFLICT DO NOTHING
        """)
        drop_staging(cur, "metric_in", "page_in", "fact_in")

    # Queue the retryable blanks. `no_tables` means screener has no page for the
    # symbol, so it is quarantined but never retried.
    queued = 0
    for sym, reason in blanks:
        if not blank_reason_is_retryable(reason):
            continue
        db.execute("""
            INSERT INTO market.fetch_retry_queue
                (source, scope, reason, attempts, state, next_attempt_at)
            VALUES ('fundamentals.screener', %s, %s, 1, 'pending', now() + interval '15 minutes')
            ON CONFLICT (source, scope) DO NOTHING
        """, (sym, reason))
        queued += 1

    close_batch(db, bid, total=len(files), ok=len(files) - len(blanks),
                failed=len(blanks), rows=len(fact_rows))
    set_watermark(db, "fundamentals.screener", "*",
                  datetime.now(IST).date().isoformat(), rows=len(fact_rows))
    return {"files": len(files), "pages": len(page_rows), "facts": len(fact_rows),
            "metrics": len(metric_rows), "blank": len(blanks), "queued": queued,
            "unmapped_symbols": unmapped}


# --------------------------------------------------------------------------
# step 4 - Yahoo weekly bars
# --------------------------------------------------------------------------

def import_weekly_prices(db: Database, data_root: Path) -> dict:
    cache = data_root / "prices"
    files = sorted(cache.glob("*.json"))
    if not files:
        return {"skipped": "no cached prices"}

    bid = open_batch(db, "legacy.yahoo_weekly", {"dir": str(cache), "files": len(files)})

    # Benchmark indices are not in EQUITY_L, so register them as securities first
    # and inside a committed transaction - the weekly_bar FK depends on them.
    bm_names: list[str] = []
    for p in files:
        if p.stem.startswith("_BM_"):
            rec = json.loads(p.read_text(encoding="utf-8"))
            bm_names.append(rec.get("benchmark_name") or p.stem.replace("_BM_", ""))
    if bm_names:
        with db.transaction() as conn, conn.cursor() as cur:
            for name in sorted(set(bm_names)):
                cur.execute("""
                    INSERT INTO market.security
                        (symbol, exchange, series, security_type, company_name, is_active)
                    VALUES (%s, 'NSE', 'IDX', 'index', %s, true)
                    ON CONFLICT (symbol, exchange) DO UPDATE SET last_seen_at = now()
                """, (name, name.replace("_", " ").title()))

    smap = symbol_id_map(db)

    rows: list[tuple] = []
    errors: list[tuple[str, str]] = []
    benchmarks = 0
    series_loaded = 0

    for p in files:
        rec = json.loads(p.read_text(encoding="utf-8"))
        if rec.get("error"):
            errors.append((p.stem, str(rec["error"])))
            continue
        ts = rec.get("timestamp") or []
        if not ts:
            errors.append((p.stem, "no bars"))
            continue

        if p.stem.startswith("_BM_"):
            name = rec.get("benchmark_name") or p.stem.replace("_BM_", "")
            sid = smap.get(name)
            if sid is None:
                errors.append((p.stem, "benchmark not registered"))
                continue
            benchmarks += 1
        else:
            sid = smap.get(rec.get("nse_symbol") or p.stem)
            if sid is None:
                errors.append((p.stem, "symbol not in security master"))
                continue

        df = pd.DataFrame({
            "ts": pd.to_datetime(ts, unit="s", utc=True),
            "open": rec.get("open"), "high": rec.get("high"),
            "low": rec.get("low"), "close": rec.get("close"),
            "volume": rec.get("volume"),
            "adjclose": rec.get("adjclose") or rec.get("close"),
        }).dropna(subset=["adjclose"])
        if df.empty:
            errors.append((p.stem, "no adjusted closes"))
            continue

        # Same OHLC scaling the legacy loader applied, so imported bars are
        # numerically identical to what the frozen baseline was computed from.
        factor = (df["adjclose"] / df["close"].replace(0, pd.NA)).fillna(1.0)
        for c in ("open", "high", "low"):
            df[c] = pd.to_numeric(df[c], errors="coerce") * factor

        df["local"] = df["ts"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize()
        today = datetime.now(IST).date()
        for r in df.itertuples(index=False):
            d = r.local.date()
            we = iso_friday(d)
            iso = we.isocalendar()
            rows.append((
                sid, we, iso.year, iso.week,
                None if pd.isna(r.open) else float(r.open),
                None if pd.isna(r.high) else float(r.high),
                None if pd.isna(r.low) else float(r.low),
                float(r.adjclose),
                None if pd.isna(r.volume) else int(r.volume),
                None, 5, "yahoo_weekly", 50, "yahoo_adjclose",
                # The current week's Friday has not happened yet; that bar holds
                # a partial week and must not be treated as a closed one.
                we <= today, d))
        series_loaded += 1

    cols = ("security_id", "week_end_date", "iso_year", "iso_week", "open", "high",
            "low", "close", "volume", "turnover_inr", "day_count", "source",
            "source_rank", "adj_basis", "is_complete", "last_trade_date")
    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "weekly_in", {
            "security_id": "bigint", "week_end_date": "date", "iso_year": "smallint",
            "iso_week": "smallint", "open": "numeric", "high": "numeric",
            "low": "numeric", "close": "numeric", "volume": "bigint",
            "turnover_inr": "numeric", "day_count": "smallint", "source": "text",
            "source_rank": "smallint", "adj_basis": "text",
            "is_complete": "boolean", "last_trade_date": "date"})
        copy_rows(cur, "weekly_in", cols, rows)
        # Rows are kept per source; weekly_bar_resolved picks the winner. Both
        # remain so the two series can be reconciled against each other.
        cur.execute(f"""
            INSERT INTO market.weekly_bar ({", ".join(cols)})
            SELECT DISTINCT ON (security_id, week_end_date, source) {", ".join(cols)}
            FROM   staging.weekly_in
            ON CONFLICT (security_id, week_end_date, source) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume,
                source_rank = EXCLUDED.source_rank,
                adj_basis = EXCLUDED.adj_basis, computed_at = now()
        """)
        drop_staging(cur, "weekly_in")

    for sym, err in errors:
        record_error(db, bid, "legacy.yahoo_weekly", sym, err)

    close_batch(db, bid, total=len(files), ok=series_loaded, failed=len(errors),
                rows=len(rows))
    set_watermark(db, "prices.yahoo_weekly", "*",
                  datetime.now(IST).date().isoformat(), rows=len(rows))
    return {"files": len(files), "series": series_loaded, "benchmarks": benchmarks,
            "bars": len(rows), "errors": len(errors)}


# --------------------------------------------------------------------------
# step 5 - announcements
# --------------------------------------------------------------------------

def import_announcements(db: Database, data_root: Path,
                         chunk_size: int = 50_000) -> dict:
    p = data_root / "raw" / "nse_announcements.csv"
    if not p.exists():
        return {"skipped": "no cached announcements"}

    smap = symbol_id_map(db)
    bid = open_batch(db, "legacy.announcements", {"file": str(p)})
    total = 0

    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "ann_in", {
            "announcement_hash": "text", "security_id": "bigint", "raw_symbol": "text",
            "raw_isin": "text", "source": "text", "external_id": "text",
            "subject": "text", "description": "text", "announced_at": "timestamptz",
            "available_at": "timestamptz", "attachment_url": "text"})

        for chunk in pd.read_csv(p, chunksize=chunk_size, low_memory=False):
            chunk.columns = [c.strip() for c in chunk.columns]
            rows = []
            for r in chunk.itertuples(index=False):
                sym = str(getattr(r, "symbol", "") or "").strip()
                if not sym or sym == "nan":
                    continue
                subject = getattr(r, "desc", None)
                body = getattr(r, "attchmntText", None)
                att = getattr(r, "attchmntFile", None)
                when = getattr(r, "an_dt", None) or getattr(r, "dt", None)
                announced = pd.to_datetime(when, errors="coerce", dayfirst=False)
                if pd.isna(announced):
                    announced = None
                else:
                    announced = announced.to_pydatetime().replace(tzinfo=IST)
                h = _sha("nse_announcements", sym, _norm_text(subject),
                         announced.isoformat() if announced else "",
                         att, getattr(r, "seqId", None))
                rows.append((
                    h, smap.get(sym), sym,
                    str(getattr(r, "sm_isin", "") or "").strip() or None,
                    "nse_announcements",
                    str(getattr(r, "seqId", "") or "").strip() or None,
                    str(subject)[:2000] if subject and str(subject) != "nan" else None,
                    str(body)[:8000] if body and str(body) != "nan" else None,
                    announced,
                    announced,   # knowledge date == disclosure time for this feed
                    str(att) if att and str(att) != "nan" else None))
            if rows:
                total += copy_rows(cur, "ann_in", (
                    "announcement_hash", "security_id", "raw_symbol", "raw_isin",
                    "source", "external_id", "subject", "description",
                    "announced_at", "available_at", "attachment_url"), rows)

        # seen_count is genuinely persisted here, unlike market_intel where it was
        # only ever incremented on an in-memory object.
        cur.execute("""
            INSERT INTO market.announcement
                (announcement_hash, security_id, raw_symbol, raw_isin, source,
                 external_id, subject, description, announced_at, available_at,
                 attachment_url, sync_batch_id)
            SELECT DISTINCT ON (announcement_hash)
                   announcement_hash, security_id, raw_symbol, raw_isin, source,
                   external_id, subject, description, announced_at, available_at,
                   attachment_url, %s
            FROM   staging.ann_in
            WHERE  available_at IS NOT NULL
            ON CONFLICT (announcement_hash) DO UPDATE SET
                seen_count   = market.announcement.seen_count + 1,
                last_seen_at = now()
        """, (bid,))
        drop_staging(cur, "ann_in")

    stored = db.fetch_value("SELECT count(*) AS c FROM market.announcement")
    latest = db.fetch_value("SELECT max(announced_at) AS m FROM market.announcement")
    close_batch(db, bid, total=total, ok=total, rows=stored)
    set_watermark(db, "events.nse_announcements", "*",
                  latest.date().isoformat() if latest else None, rows=stored)
    return {"csv_rows": total, "stored": stored,
            "latest": str(latest) if latest else None}


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

STEPS = {
    "universe": import_universe,
    "bhavcopy": import_bhavcopy_cache,
    "fundamentals": import_fundamentals,
    "prices": import_weekly_prices,
    "announcements": import_announcements,
}


def run(db: Database, data_root: Path, steps: list[str] | None = None) -> dict:
    # A batch left 'running' belongs to a process that died; resolve it before
    # starting so the batch table reflects reality rather than accumulating
    # permanently ambiguous rows.
    mark_stale_batches(db, minutes=0)

    out: dict[str, Any] = {}
    for name in (steps or list(STEPS)):
        fn = STEPS[name]
        log.info("import step: %s", name)
        out[name] = fn(db, data_root)
        log.info("  %s -> %s", name, out[name])
    return out
