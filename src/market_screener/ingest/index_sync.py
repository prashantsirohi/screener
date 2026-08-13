"""
Sync exchange-published index closes into price_daily.

Indices flow through exactly the same machinery as equities: no corporate
actions, so the adjustment factor stays 1.0, and the weekly resample produces a
price-return benchmark on the `split_bonus` basis. That is what lets the
technical layer move off Yahoo.

Reuses the equity trading calendar - the exchange publishes both files on the
same sessions - so a day already known to be non-trading is never probed twice.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from ..sources.nse_indices import (INDEX_SYMBOLS, IndexCloseCollector,
                                   IndexCloseUnavailable)
from .price_sync import _is_settled
from .sync_state import (close_batch, get_watermark, mark_stale_batches,
                         open_batch, record_error, set_watermark)

log = logging.getLogger(__name__)

SOURCE = "prices.nse_index_close"

COLS = ("security_id", "trade_date", "open", "high", "low", "close",
        "prev_close", "volume", "turnover_inr", "trade_count", "series", "source")
STAGING = {
    "security_id": "bigint", "trade_date": "date", "open": "numeric",
    "high": "numeric", "low": "numeric", "close": "numeric", "prev_close": "numeric",
    "volume": "bigint", "turnover_inr": "numeric", "trade_count": "bigint",
    "series": "text", "source": "text",
}


def ensure_index_securities(db: Database) -> dict[str, int]:
    """Register the tracked indices, then return symbol -> security_id."""
    with db.transaction() as conn, conn.cursor() as cur:
        for sym in sorted(set(INDEX_SYMBOLS.values())):
            cur.execute("""
                INSERT INTO market.security
                    (symbol, exchange, series, security_type, company_name, is_active)
                VALUES (%s, 'NSE', 'IDX', 'index', %s, true)
                ON CONFLICT (symbol, exchange) DO UPDATE SET last_seen_at = now()
            """, (sym, sym.replace("_", " ").title()))
    rows = db.fetch_all(
        "SELECT symbol, security_id FROM market.security "
        "WHERE exchange = 'NSE' AND security_type = 'index'")
    return {r["symbol"]: r["security_id"] for r in rows}


def _trading_days(db: Database, start: date, until: date) -> list[date]:
    """
    Sessions the equity calendar already knows about.

    Index closes publish on the same days, so there is no reason to re-probe
    holidays the bhavcopy sync has already established.
    """
    rows = db.fetch_all("""
        SELECT trade_date FROM market.trading_calendar
        WHERE  exchange = 'NSE' AND is_trading
          AND  trade_date BETWEEN %s AND %s
        ORDER  BY trade_date
    """, (start, until))
    return [r["trade_date"] for r in rows]


def sync(db: Database, *, backfill_days: int | None = None,
         until: date | None = None, max_days: int | None = None,
         force: bool = False) -> dict:
    mark_stale_batches(db)
    until = until or date.today()

    wm = get_watermark(db, SOURCE)
    if backfill_days:
        start = until - timedelta(days=backfill_days)
    elif wm:
        start = date.fromisoformat(wm) + timedelta(days=1)
    else:
        start = until - timedelta(days=30)

    smap = ensure_index_securities(db)
    sids = tuple(smap.values())

    have: set[date] = set()
    if not force:
        rows = db.fetch_all("""
            SELECT DISTINCT trade_date FROM market.price_daily
            WHERE  source = %s AND security_id = ANY(%s)
        """, (SOURCE, list(sids)))
        have = {r["trade_date"] for r in rows}

    candidates = [d for d in _trading_days(db, start, until) if d not in have]
    if max_days:
        candidates = candidates[:max_days]
    if not candidates:
        return {"status": "current", "watermark": wm, "days_fetched": 0}

    bid = open_batch(db, SOURCE, {"start": str(start), "until": str(until),
                                  "candidates": len(candidates)})
    collector = IndexCloseCollector()
    fetched = missing = failed = pending = 0
    rows_total = 0

    for d in candidates:
        try:
            day = collector.fetch(d)
        except IndexCloseUnavailable:
            if not _is_settled(d):
                pending += 1
                continue
            missing += 1
            log.info("no index close file for %s", d)
            continue
        except Exception as exc:                      # noqa: BLE001 - isolate the day
            record_error(db, bid, SOURCE, d.isoformat(), str(exc),
                         error_class=type(exc).__name__)
            failed += 1
            continue

        out = []
        for r in day.frame.itertuples(index=False):
            sid = smap.get(r.symbol)
            if sid is None:
                continue

            def n(v):
                return None if v is None or pd.isna(v) else float(v)

            out.append((sid, d, n(r.open), n(r.high), n(r.low), n(r.close), None,
                        None if pd.isna(r.volume) else int(r.volume),
                        n(r.turnover_inr), None, "IDX", SOURCE))
        if not out:
            continue

        with db.transaction() as conn, conn.cursor() as cur:
            create_staging(cur, "idx_px", STAGING)
            copy_rows(cur, "idx_px", COLS, out)
            cur.execute(f"""
                INSERT INTO market.price_daily ({", ".join(COLS)})
                SELECT DISTINCT ON (security_id, trade_date) {", ".join(COLS)}
                FROM   staging.idx_px
                ON CONFLICT (security_id, trade_date) DO NOTHING
            """)
            drop_staging(cur, "idx_px")
        rows_total += len(out)
        fetched += 1
        if fetched % 50 == 0:
            log.info("index closes: %d/%d days", fetched, len(candidates))

    high_water = db.fetch_value(
        "SELECT max(trade_date) AS m FROM market.price_daily WHERE source = %s",
        (SOURCE,))
    status = "complete" if failed == 0 else "partial"
    close_batch(db, bid, status=status, total=len(candidates), ok=fetched,
                failed=failed, skipped=missing, rows=rows_total)
    set_watermark(db, SOURCE, "*",
                  high_water.isoformat() if high_water else None,
                  status=status, rows=rows_total)

    covered = db.fetch_value("""
        SELECT count(DISTINCT security_id) AS c FROM market.price_daily
        WHERE  source = %s
    """, (SOURCE,))
    return {"status": status, "considered": len(candidates), "days_fetched": fetched,
            "no_file": missing, "not_yet_published": pending, "failed": failed,
            "rows": rows_total, "indices_covered": covered,
            "watermark": str(high_water) if high_water else None}
