"""
Source IDs minted from real ingest provenance.

The legacy screen hardcoded its source log: a fixed list of URLs and a synthetic
`<SYM>-SCR-01` per company, asserting what the data "would have" come from. That
is a description of intent, not evidence - it stayed identical whether a page was
fetched today, six months ago, or recovered from a blank shell three attempts
later.

Here every source ID resolves to something the store actually recorded: a
sync_batch, a screener_page_raw row with its fetch timestamp, the bhavcopy day a
price came from, the announcement that raised an event flag. An auditor can
follow any figure back to the fetch that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime

from ..db.connection import Database


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    company: str | None
    symbol: str | None
    document_type: str
    title: str
    issuer: str
    published_date: date | None
    period_covered: str | None
    url: str | None
    primary_or_secondary: str
    accessed_date: date | None
    claim_or_metric_supported: str
    notes: str | None = None
    security_id: int | None = None

    def as_row(self) -> dict:
        return asdict(self)


# Exchange-level sources, recorded once per run from the actual sync batches.
def global_sources(db: Database, as_of: date,
                   cutoff: datetime | None = None) -> list[SourceRecord]:
    """
    Corpus-level source records.

    `cutoff` is the run's point-in-time boundary. Provenance has to respect it
    for the same reason the metrics do, and arguably more so: a source log that
    cites a page fetched after the as_of it claims to document is not evidence,
    it is a contradiction.
    """
    out: list[SourceRecord] = []

    uni = db.fetch_one("""
        SELECT watermark, last_run_at FROM market.sync_watermark
        WHERE source IN ('legacy.universe', 'universe.nse_equity_l')
        ORDER BY updated_at DESC LIMIT 1
    """)
    if uni:
        out.append(SourceRecord(
            source_id=f"NSE-EQL-{uni['watermark']}",
            company="ALL", symbol="ALL",
            document_type="Exchange master list",
            title="EQUITY_L.csv - securities available for trading",
            issuer="National Stock Exchange of India",
            published_date=date.fromisoformat(str(uni["watermark"])),
            period_covered=str(uni["watermark"]),
            url="https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            primary_or_secondary="Primary",
            accessed_date=uni["last_run_at"].date() if uni["last_run_at"] else None,
            claim_or_metric_supported=(
                "Universe definition; listed symbols, ISIN, listing date, face value"),
            notes="Defines the active screening universe"))

    px = db.fetch_one("""
        SELECT min(trade_date) AS first_day, max(trade_date) AS last_day,
               count(DISTINCT trade_date) AS days, count(*) AS rows
        FROM   market.price_daily
        WHERE  source = 'nse_bhavcopy' AND trade_date <= %s
    """, (as_of,))
    if px and px["days"]:
        out.append(SourceRecord(
            source_id=f"NSE-BHAV-{px['first_day']}-{px['last_day']}",
            company="ALL", symbol="ALL",
            document_type="Exchange bhavcopy",
            title=f"NSE daily bhavcopy, {px['days']} sessions",
            issuer="National Stock Exchange of India",
            published_date=px["last_day"], period_covered=f"{px['first_day']} to {px['last_day']}",
            url="https://nsearchives.nseindia.com/content/cm/",
            primary_or_secondary="Primary", accessed_date=as_of,
            claim_or_metric_supported="Daily OHLC, traded volume and turnover",
            notes=f"{px['rows']:,} daily bars"))

    ann = db.fetch_one("""
        SELECT min(announced_at)::date AS f, max(announced_at)::date AS l, count(*) AS n
        FROM   market.announcement
        WHERE  (%s::timestamptz IS NULL OR announced_at < %s::timestamptz)
    """, (cutoff, cutoff))
    if ann and ann["n"]:
        out.append(SourceRecord(
            source_id=f"NSE-ANN-{ann['f']}-{ann['l']}",
            company="ALL", symbol="ALL",
            document_type="Corporate announcements",
            title="NSE corporate announcements (equities)",
            issuer="National Stock Exchange of India",
            published_date=ann["l"], period_covered=f"{ann['f']} to {ann['l']}",
            url="https://www.nseindia.com/companies-listing/corporate-filings-announcements",
            primary_or_secondary="Primary", accessed_date=as_of,
            claim_or_metric_supported=(
                "Event-driven archetype; demerger, open-offer, asset-sale and "
                "governance flags"),
            notes=f"{ann['n']:,} announcements classified"))

    ca = db.fetch_one("""
        SELECT count(*) AS n, count(*) FILTER (WHERE source='nse_api') AS reported
        FROM   market.corporate_action
    """)
    if ca and ca["n"]:
        out.append(SourceRecord(
            source_id="NSE-CA-FEED",
            company="ALL", symbol="ALL",
            document_type="Corporate actions",
            title="NSE corporate actions (splits, bonuses)",
            issuer="National Stock Exchange of India",
            published_date=None, period_covered=None,
            url="https://www.nseindia.com/companies-listing/corporate-filings-actions",
            primary_or_secondary="Primary", accessed_date=as_of,
            claim_or_metric_supported="Split/bonus adjustment factors for the price series",
            notes=f"{ca['reported']} reported by the feed of {ca['n']} total"))

    bm = db.fetch_one("""
        SELECT count(*) AS bars FROM market.weekly_bar w
        JOIN market.security s USING (security_id)
        WHERE s.symbol = 'NIFTY_500'
    """)
    if bm and bm["bars"]:
        out.append(SourceRecord(
            source_id="YF-BENCH-N500",
            company="Nifty 500 index", symbol="NIFTY_500",
            document_type="Market data",
            title="Nifty 500 adjusted weekly series",
            issuer="Yahoo Finance", published_date=None, period_covered=None,
            url="https://query1.finance.yahoo.com/v8/finance/chart/%5ECRSLDX",
            primary_or_secondary="Secondary", accessed_date=as_of,
            claim_or_metric_supported="Relative-strength benchmark for Weinstein staging",
            notes=f"{bm['bars']} weekly bars"))
    return out


def company_sources(db: Database, security_ids: list[int],
                    as_of: date,
                    cutoff: datetime | None = None) -> list[SourceRecord]:
    """
    Per-company sources, keyed to the actual rows that produced the numbers.

    The fundamentals ID carries the fetch date, so a page recovered from a blank
    shell three days later is distinguishable from one fetched in the first pass.
    """
    if not security_ids:
        return []
    out: list[SourceRecord] = []

    pages = db.fetch_all("""
        SELECT DISTINCT ON (p.security_id)
               p.security_id, s.symbol, s.company_name, p.basis, p.source_url,
               p.fetched_at, p.is_blank
        FROM   market.screener_page_raw p
        JOIN   market.security s USING (security_id)
        WHERE  p.security_id = ANY(%s) AND NOT p.is_blank
          AND  (%s::timestamptz IS NULL OR p.fetched_at < %s::timestamptz)
        ORDER  BY p.security_id, p.fetched_at DESC
    """, (security_ids, cutoff, cutoff))
    for p in pages:
        d = p["fetched_at"].date()
        out.append(SourceRecord(
            source_id=f"{p['symbol']}-SCR-{d.isoformat()}",
            company=p["company_name"], symbol=p["symbol"], security_id=p["security_id"],
            document_type="Aggregated financial statements",
            title=f"screener.in company page ({p['basis']} basis)",
            issuer="screener.in", published_date=d, period_covered=None,
            url=p["source_url"], primary_or_secondary="Secondary", accessed_date=d,
            claim_or_metric_supported=(
                "Annual/quarterly P&L, balance sheet, cash flow, ratios, shareholding"),
            notes=("Aggregator of audited filings - material claims must be "
                   "confirmed against the filing itself in Phase 2")))

    bars = db.fetch_all("""
        SELECT c.security_id, s.symbol, s.company_name, c.source, c.adj_basis,
               c.bars, c.first_week, c.last_week
        FROM   market.weekly_bar_source_choice c
        JOIN   market.security s USING (security_id)
        WHERE  c.security_id = ANY(%s)
    """, (security_ids,))
    for b in bars:
        primary = b["source"] == "nse_bhavcopy"
        out.append(SourceRecord(
            source_id=f"{b['symbol']}-PXW-{b['source']}",
            company=b["company_name"], symbol=b["symbol"], security_id=b["security_id"],
            document_type="Market data",
            title=f"{b['symbol']} weekly bars ({b['adj_basis']} basis)",
            issuer=("National Stock Exchange of India" if primary else "Yahoo Finance"),
            published_date=b["last_week"],
            period_covered=f"{b['first_week']} to {b['last_week']}",
            url=None, primary_or_secondary="Primary" if primary else "Secondary",
            accessed_date=as_of,
            claim_or_metric_supported=(
                "Weinstein stage, 30/40w MA, relative strength, base, volume, liquidity"),
            notes=f"{b['bars']} complete weekly bars, elected source"))

    events = db.fetch_all("""
        SELECT a.security_id, s.symbol, s.company_name,
               count(DISTINCT c.primary_category) AS classes,
               max(a.announced_at)::date          AS latest
        FROM   market.announcement a
        JOIN   market.announcement_classification c USING (announcement_hash)
        JOIN   market.security s ON s.security_id = a.security_id
        WHERE  a.security_id = ANY(%s) AND c.taxonomy_version LIKE 'v1:%%'
          AND  (%s::timestamptz IS NULL OR a.announced_at < %s::timestamptz)
        GROUP  BY 1, 2, 3
    """, (security_ids, cutoff, cutoff))
    for e in events:
        out.append(SourceRecord(
            source_id=f"{e['symbol']}-EVT-{e['latest']}",
            company=e["company_name"], symbol=e["symbol"], security_id=e["security_id"],
            document_type="Corporate announcement",
            title=f"{e['classes']} classified event type(s), latest {e['latest']}",
            issuer="National Stock Exchange of India",
            published_date=e["latest"], period_covered=None,
            url="https://www.nseindia.com/companies-listing/corporate-filings-announcements",
            primary_or_secondary="Primary", accessed_date=as_of,
            claim_or_metric_supported="Event-driven classification and governance flags",
            notes="Keyword-classified; confirm the underlying intimation in Phase 2"))
    return out


def source_ids_for(records: list[SourceRecord], security_id: int
                   ) -> tuple[str, str]:
    """(primary_source_ids, secondary_source_ids) for one company, pipe-joined."""
    mine = [r for r in records if r.security_id == security_id]
    glob = [r for r in records if r.security_id is None]
    prim = [r.source_id for r in glob + mine if r.primary_or_secondary == "Primary"]
    sec = [r.source_id for r in glob + mine if r.primary_or_secondary == "Secondary"]
    return "|".join(prim), "|".join(sec)
