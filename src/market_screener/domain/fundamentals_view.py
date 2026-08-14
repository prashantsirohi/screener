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

    # The cutoff is in the statement unconditionally, as a NULL-tolerant
    # predicate, rather than appended only when as_of is supplied. Same result,
    # but the bound is now visible in the SQL itself instead of depending on
    # every caller remembering to ask for it - which is precisely how the screen
    # came to read facts from the future for as long as it did.
    params: list[Any] = [security_id, as_of, as_of]
    sql = """
        SELECT DISTINCT ON (period_type, report_date, statement_basis, metric_id)
               period_type, report_date, statement_basis, metric_id, value, available_at
        FROM   market.screener_fact
        WHERE  security_id = %s
          AND  (%s::timestamptz IS NULL OR available_at <= %s::timestamptz)
    """
    if basis is not None:
        sql += " AND statement_basis = %s"
        params.append(basis)
    sql += (" ORDER BY period_type, report_date, statement_basis, metric_id, "
            "available_at DESC")

    return _assemble(db.fetch_all(sql, params), labels)


def _assemble(rows, labels: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """
    Fact rows -> the parsed-page dict shape.

    Shared by the single-security and bulk paths so the two cannot drift. The
    bulk path exists purely to avoid 2N round trips; if it reconstructed the
    payload by its own slightly different logic, the saving would be paid for in
    answers that differ from the parity baseline for no visible reason.

    Rows must arrive already ordered and de-duplicated by the caller's query.
    """
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


def payloads_for_universe(db: Database, *,
                          as_of: datetime | None = None,
                          labels: dict[str, tuple[str, str]] | None = None,
                          ) -> dict[int, dict[str, Any]]:
    """
    Every active security's payload, in two queries instead of 2N.

    `payload_for_metrics` issues one metadata query and one fact query per
    company. Measured over the 2,086-name universe that is 4,172 round trips
    costing 340 of s80's 485 seconds, while the arithmetic those payloads feed
    takes 1.3 seconds in total. The cost was never the computation.

    Returns the same dicts `payload_for_metrics` returns, keyed by security_id,
    including the `error` records for blank and unknown pages - a caller must
    not have to distinguish "no payload" from "payload absent from the map".
    `test_bulk_payloads_match_single` asserts the equivalence over the whole
    universe rather than a sample.
    """
    from collections import defaultdict

    labels = labels if labels is not None else _label_lookup(db)

    meta_rows = db.fetch_all("""
        SELECT s.security_id, s.symbol, s.company_name,
               p.basis, p.source_url, p.is_blank
        FROM   market.security s
        LEFT JOIN LATERAL (
            SELECT basis, source_url, is_blank
            FROM   market.screener_page_raw
            WHERE  security_id = s.security_id
              AND  (%s::timestamptz IS NULL OR fetched_at <= %s::timestamptz)
            ORDER  BY fetched_at DESC LIMIT 1
        ) p ON true
        WHERE  s.is_active AND s.series = 'EQ' AND s.security_type = 'equity'
    """, (as_of, as_of))

    # statement_basis is NOT filtered here: it varies per security, so the
    # single-security query's `AND statement_basis = %s` has no bulk equivalent.
    # It is in the DISTINCT ON key, so both bases survive and each security is
    # narrowed to its own below - same rows, one scan.
    fact_rows = db.fetch_all("""
        SELECT DISTINCT ON (security_id, period_type, report_date,
                            statement_basis, metric_id)
               security_id, period_type, report_date, statement_basis,
               metric_id, value
        FROM   market.screener_fact
        WHERE  (%s::timestamptz IS NULL OR available_at <= %s::timestamptz)
        ORDER  BY security_id, period_type, report_date, statement_basis,
                  metric_id, available_at DESC
    """, (as_of, as_of))

    by_security: dict[int, list] = defaultdict(list)
    for r in fact_rows:
        by_security[r["security_id"]].append(r)

    out: dict[int, dict[str, Any]] = {}
    for m in meta_rows:
        sid = m["security_id"]
        if m["is_blank"]:
            out[sid] = {"symbol": m["symbol"], "error": "blank_page"}
            continue
        basis = m["basis"]
        rows = by_security.get(sid, ())
        if basis is not None:
            rows = [r for r in rows if r["statement_basis"] == basis]
        rec = _assemble(rows, labels)
        rec["symbol"] = m["symbol"]
        rec["name"] = m["company_name"]
        rec["basis"] = basis
        rec["source_url"] = m["source_url"]
        out[sid] = rec
    return out


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
