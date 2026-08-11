"""
Incremental daily-price sync from NSE bhavcopy.

Watermark is `MAX(trade_date)` in price_daily. The sync walks forward one
session at a time; a weekday with no published bhavcopy is recorded as a
non-trading day, which is how the trading calendar builds itself from what the
exchange actually did rather than from a hardcoded holiday list.

Failure is isolated per date: one unreadable day is an error row, not an aborted
backfill.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

from ..db.connection import Database
from ..db.copy_io import copy_rows, create_staging, drop_staging
from ..sources.nse_bhavcopy import BhavcopyCollector, BhavcopyUnavailable
from .sync_state import (close_batch, get_watermark, mark_stale_batches,
                         open_batch, record_error, set_watermark)

log = logging.getLogger(__name__)

SOURCE = "prices.nse_bhavcopy"

IST = timezone(timedelta(hours=5, minutes=30))
# NSE closes at 15:30 IST and the full bhavcopy lands well after. Before this
# time a missing file means "not published yet", not "market holiday" - marking
# it a holiday would exclude the session from every future sync.
BHAVCOPY_PUBLISHED_AFTER = time(19, 0)


def _is_settled(d: date, now: datetime | None = None) -> bool:
    """True when a missing bhavcopy for `d` can be trusted to mean a holiday."""
    now = now or datetime.now(IST)
    if d < now.date():
        return True
    if d > now.date():
        return False
    return now.time() >= BHAVCOPY_PUBLISHED_AFTER

PRICE_COLS = ("security_id", "trade_date", "open", "high", "low", "close",
              "prev_close", "volume", "turnover_inr", "trade_count", "series", "source")

PRICE_STAGING = {
    "security_id": "bigint", "trade_date": "date", "open": "numeric",
    "high": "numeric", "low": "numeric", "close": "numeric", "prev_close": "numeric",
    "volume": "bigint", "turnover_inr": "numeric", "trade_count": "bigint",
    "series": "text", "source": "text",
}


def _symbol_map(db: Database) -> dict[str, int]:
    """
    Symbol -> security_id, including historical symbols.

    Aliases matter for a backfill: an old bhavcopy carries the symbol as it was
    on that date, and those bars belong to the same security as today's ticker.
    The current symbol wins where a string is used by both.
    """
    rows = db.fetch_all("""
        SELECT symbol, security_id, 1 AS pref
        FROM   market.security WHERE exchange = 'NSE'
        UNION ALL
        SELECT symbol, security_id, 2 AS pref
        FROM   market.security_alias WHERE exchange = 'NSE'
    """)
    out: dict[str, int] = {}
    for r in sorted(rows, key=lambda x: -x["pref"]):   # pref 1 applied last
        out[r["symbol"]] = r["security_id"]
    return out


def _known_calendar(db: Database) -> dict[date, bool]:
    rows = db.fetch_all(
        "SELECT trade_date, is_trading FROM market.trading_calendar WHERE exchange = 'NSE'")
    return {r["trade_date"]: r["is_trading"] for r in rows}


def _dates_with_prices(db: Database) -> set[date]:
    rows = db.fetch_all("SELECT DISTINCT trade_date FROM market.price_daily")
    return {r["trade_date"] for r in rows}


def _mark_calendar(db: Database, d: date, is_trading: bool, note: str | None = None) -> None:
    db.execute(
        "INSERT INTO market.trading_calendar (exchange, trade_date, is_trading, source, note) "
        "VALUES ('NSE', %s, %s, 'nse_bhavcopy_probe', %s) "
        "ON CONFLICT (exchange, trade_date) DO UPDATE SET "
        "  is_trading = EXCLUDED.is_trading, note = COALESCE(EXCLUDED.note, market.trading_calendar.note)",
        (d, is_trading, note))


def _register_new_symbols(db: Database, frame: pd.DataFrame,
                          known: dict[str, int]) -> int:
    """
    Add series-EQ symbols that appear in bhavcopy but not yet in the master.

    A company that lists between universe syncs would otherwise lose its price
    history until the next EQUITY_L refresh.
    """
    unknown = frame[~frame["symbol"].isin(known.keys())]
    if unknown.empty:
        return 0

    rows = []
    for r in unknown.drop_duplicates("symbol").itertuples(index=False):
        isin = getattr(r, "isin", None)
        isin = None if (isin is None or pd.isna(isin) or str(isin) in ("", "nan")) else str(isin)
        # is_active is FALSE here on purpose. A symbol discovered in a historical
        # bhavcopy may be delisted, merged, or moved series; only the current
        # EQUITY_L says what is tradable today, and the universe sync owns that
        # flag. Registering it merely lets the historical bars attach to a
        # security - it must not silently widen the screening universe.
        rows.append((isin, r.symbol, "NSE", "EQ", "equity", None, False))

    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "newsec_in", {
            "isin": "text", "symbol": "text", "exchange": "text", "series": "text",
            "security_type": "text", "company_name": "text", "is_active": "boolean"})
        copy_rows(cur, "newsec_in",
                  ("isin", "symbol", "exchange", "series", "security_type",
                   "company_name", "is_active"), rows)

        # A symbol we have not seen whose ISIN we already hold is a rename, not a
        # new company. Record the old ticker as an alias so its historical bars
        # attach to the existing security rather than creating a duplicate.
        cur.execute("""
            INSERT INTO market.security_alias
                (security_id, symbol, exchange, valid_from, source)
            SELECT DISTINCT s.security_id, n.symbol, n.exchange, CURRENT_DATE,
                   'bhavcopy_isin_match'
            FROM   staging.newsec_in n
            JOIN   market.security s ON s.isin = n.isin
            WHERE  n.isin IS NOT NULL
            ON CONFLICT DO NOTHING
        """)
        renamed = cur.rowcount

        # Genuinely new: no existing security shares the ISIN.
        cur.execute("""
            INSERT INTO market.security
                (isin, symbol, exchange, series, security_type, company_name, is_active)
            SELECT DISTINCT ON (n.symbol, n.exchange)
                   n.isin, n.symbol, n.exchange, n.series, n.security_type,
                   n.company_name, n.is_active
            FROM   staging.newsec_in n
            WHERE  n.isin IS NULL
               OR  NOT EXISTS (SELECT 1 FROM market.security s WHERE s.isin = n.isin)
            ON CONFLICT (symbol, exchange) DO NOTHING
        """)
        created = cur.rowcount
        drop_staging(cur, "newsec_in")

    if created or renamed:
        log.info("bhavcopy symbols: %d new securit(ies), %d alias(es) for renames",
                 created, renamed)
    return created + renamed


def _write_day(db: Database, day, smap: dict[str, int]) -> int:
    rows = []
    for r in day.frame.itertuples(index=False):
        sid = smap.get(r.symbol)
        if sid is None:
            continue

        def n(v):
            return None if v is None or pd.isna(v) else float(v)

        def i(v):
            return None if v is None or pd.isna(v) else int(v)

        rows.append((sid, day.trade_date, n(r.open), n(r.high), n(r.low), n(r.close),
                     n(r.prev_close), i(r.volume), n(r.turnover_inr),
                     i(r.trade_count), "EQ", "nse_bhavcopy"))
    if not rows:
        return 0
    with db.transaction() as conn, conn.cursor() as cur:
        create_staging(cur, "price_in", PRICE_STAGING)
        copy_rows(cur, "price_in", PRICE_COLS, rows)
        cur.execute(f"""
            INSERT INTO market.price_daily ({", ".join(PRICE_COLS)})
            SELECT DISTINCT ON (security_id, trade_date) {", ".join(PRICE_COLS)}
            FROM   staging.price_in
            ON CONFLICT (security_id, trade_date) DO NOTHING
        """)
        drop_staging(cur, "price_in")
    return len(rows)


def sync(db: Database, *, backfill_days: int | None = None,
         until: date | None = None, max_days: int | None = None,
         force: bool = False) -> dict:
    """
    Walk forward from the watermark to `until`, one session per iteration.

    `backfill_days` overrides the watermark and starts that many calendar days
    back - used for the initial history load.
    """
    mark_stale_batches(db)
    until = until or date.today()

    wm = get_watermark(db, SOURCE)
    if backfill_days:
        start = until - timedelta(days=backfill_days)
    elif wm:
        start = date.fromisoformat(wm) + timedelta(days=1)
    else:
        start = until - timedelta(days=30)

    if start > until:
        log.info("prices already current through %s", wm)
        return {"status": "current", "watermark": wm, "days_fetched": 0}

    calendar = _known_calendar(db)
    have = set() if force else _dates_with_prices(db)

    # Every calendar day is probed, not just weekdays.
    #
    # NSE trades some Saturdays and Sundays - disaster-recovery sessions and
    # Diwali Muhurat. Probing 2024-01-20, 2023-11-12, 2024-03-02 and 2024-05-18
    # found a published bhavcopy on all four. Skipping weekends leaves holes that
    # corrupt weekly bars and make the prev-close inference read a missing
    # session as a corporate action. The calendar caches each answer, so the
    # extra probes happen once and later syncs skip known non-trading days.
    candidates: list[date] = []
    d = start
    while d <= until:
        if calendar.get(d) is False:              # known non-trading day
            pass
        elif d in have:                           # already loaded
            pass
        else:
            candidates.append(d)
        d += timedelta(days=1)
    if max_days:
        candidates = candidates[:max_days]

    bid = open_batch(db, SOURCE, {"start": str(start), "until": str(until),
                                  "candidates": len(candidates), "force": force})
    collector = BhavcopyCollector()
    smap = _symbol_map(db)

    fetched = holidays = failed = pending = 0
    rows_total = 0
    newest: date | None = None

    for d in candidates:
        try:
            day = collector.fetch(d)
        except BhavcopyUnavailable:
            if not _is_settled(d):
                # Not published yet. Leave the calendar alone so the session is
                # retried later instead of being written off as a holiday.
                pending += 1
                log.info("bhavcopy for %s not published yet - will retry", d)
                continue
            _mark_calendar(db, d, False, "no bhavcopy published")
            holidays += 1
            log.info("no bhavcopy for %s - recorded as a non-trading day", d)
            continue
        except Exception as exc:                   # noqa: BLE001 - isolate the day
            record_error(db, bid, SOURCE, d.isoformat(), str(exc),
                         error_class=type(exc).__name__)
            failed += 1
            log.warning("bhavcopy %s failed: %s", d, exc)
            continue

        added = _register_new_symbols(db, day.frame, smap)
        if added:
            smap = _symbol_map(db)

        n = _write_day(db, day, smap)
        _mark_calendar(db, d, True, f"{day.layout} layout")
        rows_total += n
        fetched += 1
        newest = d if newest is None or d > newest else newest
        log.info("bhavcopy %s (%s): %d rows", d, day.layout, n)

    high_water = db.fetch_value("SELECT max(trade_date) AS m FROM market.price_daily")
    status = "complete" if failed == 0 else "partial"
    close_batch(db, bid, status=status, total=len(candidates), ok=fetched,
                failed=failed, skipped=holidays, rows=rows_total)
    set_watermark(db, SOURCE, "*",
                  high_water.isoformat() if high_water else None,
                  status=status, rows=rows_total)

    return {"status": status, "considered": len(candidates), "days_fetched": fetched,
            "holidays": holidays, "not_yet_published": pending, "failed": failed,
            "rows": rows_total,
            "watermark": str(high_water) if high_water else None}
