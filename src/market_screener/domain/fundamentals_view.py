"""
Rebuild the legacy payload shape from the point-in-time EAV store.

`metrics.compute()` is ~280 lines of statement arithmetic that has already been
validated against a frozen baseline. Rather than rewrite it against a new data
shape - and risk changing answers invisibly - the store is read back into the
exact dict the legacy parser produced, and the arithmetic ports verbatim.

The reconstruction is also the proof that the EAV load is lossless: a test
asserts the rebuilt payload equals the retained raw payload for a sample of
companies. If the two ever diverge, the fact table lost something.

as_of matters. Passing an `available_at` cutoff yields the fundamentals as they
were known on that date, which is what makes a historical run honest.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..db.connection import Database
from . import metric_map as mm
from .periods import format_period_label as period_label


def _label_lookup(db: Database) -> dict[str, tuple[str, str]]:
    """metric_id -> (statement, original label)."""
    rows = db.fetch_all("SELECT metric_id, statement, metric_label FROM market.metric_dim")
    return {r["metric_id"]: (r["statement"], r["metric_label"]) for r in rows}


def reconstruct_payload(db: Database, security_id: int, *,
                        as_of: datetime | None = None,
                        basis: str | None = None,
                        labels: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """
    Rebuild the parsed-page dict for one security from screener_fact.

    Only facts known at `as_of` are included, so a historical screen sees the
    numbers as they stood then rather than as they were later restated.
    """
    labels = labels or _label_lookup(db)

    params: list[Any] = [security_id]
    sql = """
        SELECT DISTINCT ON (period_type, report_date, statement_basis, metric_id)
               period_type, report_date, statement_basis, metric_id, value, available_at
        FROM   market.screener_fact
        WHERE  security_id = %s
    """
    if as_of is not None:
        sql += " AND available_at <= %s"
        params.append(as_of)
    if basis is not None:
        sql += " AND statement_basis = %s"
        params.append(basis)
    sql += (" ORDER BY period_type, report_date, statement_basis, metric_id, "
            "available_at DESC")

    rows = db.fetch_all(sql, params)

    out: dict[str, Any] = {
        "profit_loss": {}, "balance_sheet": {}, "cash_flow": {},
        "ratios": {}, "quarters": {}, "shareholding": {},
        "top_ratios": {}, "growth": {},
    }

    for r in rows:
        mid = r["metric_id"]
        known = labels.get(mid)
        if not known:
            continue
        statement, label = known
        value = float(r["value"]) if r["value"] is not None else None

        if statement == "top_ratios":
            out["top_ratios"][label] = value
            continue

        if statement == "growth":
            # metric_label is stored as "<table> :: <range>"; split it back.
            table, _, rng = label.partition(" :: ")
            out["growth"].setdefault(table, {})[rng] = value
            continue

        block = out.setdefault(statement, {})
        series = block.setdefault(label, {})
        series[period_label(r["report_date"], r["period_type"])] = value

    # TTM is stored against the latest quarter end but belongs on the annual
    # statements in the payload shape, which is where the parser put it.
    return out


def payload_for_metrics(db: Database, security_id: int, *,
                        as_of: datetime | None = None,
                        labels: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """Reconstructed payload plus the identity fields metrics.compute() reads."""
    meta = db.fetch_one("""
        SELECT s.symbol, s.company_name, p.basis, p.source_url, p.is_blank
        FROM   market.security s
        LEFT JOIN LATERAL (
            SELECT basis, source_url, is_blank
            FROM   market.screener_page_raw
            WHERE  security_id = s.security_id
              AND  (%s::timestamptz IS NULL OR fetched_at <= %s::timestamptz)
            ORDER  BY fetched_at DESC LIMIT 1
        ) p ON true
        WHERE s.security_id = %s
    """, (as_of, as_of, security_id))
    if not meta:
        return {"error": "unknown security"}
    if meta["is_blank"]:
        return {"symbol": meta["symbol"], "error": "blank_page"}

    rec = reconstruct_payload(db, security_id, as_of=as_of,
                              basis=meta["basis"], labels=labels)
    rec["symbol"] = meta["symbol"]
    rec["name"] = meta["company_name"]
    rec["basis"] = meta["basis"]
    rec["source_url"] = meta["source_url"]
    return rec


def raw_payload(db: Database, security_id: int,
                as_of: datetime | None = None) -> dict[str, Any] | None:
    """The retained page payload, used as the oracle for reconstruction tests."""
    row = db.fetch_one("""
        SELECT payload FROM market.screener_page_raw
        WHERE  security_id = %s
          AND  (%s::timestamptz IS NULL OR fetched_at <= %s::timestamptz)
        ORDER  BY fetched_at DESC LIMIT 1
    """, (security_id, as_of, as_of))
    return row["payload"] if row else None
