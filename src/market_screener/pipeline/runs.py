"""
Run inspection and comparison.

The reason every run is retained in full: two runs can be diffed. The original
JSON-cache pipeline overwrote its own output, so "what changed since last week,
and why" was unanswerable.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from ..db.connection import Database


def list_runs(db: Database, limit: int = 20) -> list[dict]:
    return db.fetch_all("""
        SELECT r.run_id, r.phase, r.status, r.as_of_date,
               r.started_at, r.completed_at,
               (r.counts ->> 'evaluated')::int AS evaluated,
               (r.counts ->> 'eligible')::int  AS eligible,
               (r.counts ->> 'selected')::int  AS selected,
               (SELECT count(*) FROM market.screen_qc_result q
                 WHERE q.run_id = r.run_id AND NOT q.passed) AS qc_failed
        FROM   market.screen_run r
        ORDER  BY r.started_at DESC
        LIMIT  %s
    """, (limit,))


def show_run(db: Database, run_id: str) -> dict:
    run = db.fetch_one(
        "SELECT * FROM market.screen_run WHERE run_id = %s", (run_id,))
    if not run:
        return {}
    stages = db.fetch_all("""
        SELECT stage, status, skip_reason, rows_in, rows_out,
               EXTRACT(epoch FROM (finished_at - started_at))::int AS seconds, error
        FROM   market.screen_stage WHERE run_id = %s ORDER BY stage
    """, (run_id,))
    artifacts = db.fetch_all("""
        SELECT artifact_name, row_count, bytes, left(sha256, 12) AS sha
        FROM   market.screen_artifact WHERE run_id = %s ORDER BY artifact_name
    """, (run_id,))
    qc = db.fetch_all("""
        SELECT check_id, check_name, passed, detail
        FROM   market.screen_qc_result WHERE run_id = %s ORDER BY check_id
    """, (run_id,))
    archetypes = db.fetch_all("""
        SELECT primary_archetype, count(*) AS n
        FROM   market.phase1_universe
        WHERE  run_id = %s AND eligible_flag = 1
        GROUP  BY 1 ORDER BY n DESC
    """, (run_id,))
    return {"run": run, "stages": stages, "artifacts": artifacts, "qc": qc,
            "archetypes": archetypes}


# Columns whose movement is expected between runs and is not itself a finding.
_NOISE_COLS = {"primary_source_ids", "secondary_source_ids", "screening_date",
               "price_date", "market_cap_date", "technical_data_date"}


def diff_runs(db: Database, base: str, other: str,
              tolerance: float = 1e-6) -> dict:
    """
    Compare two runs' universes and candidate sets.

    Reports what entered and left the candidate list, how eligibility moved, and
    which fields changed for companies present in both - so a difference can be
    attributed rather than guessed at.
    """
    cols = ("symbol, company, eligible_flag, exclusion_code, primary_archetype, "
            "secondary_tags, technical_stage, preliminary_priority_score, "
            "market_cap_inr_cr, revenue_cagr_5y_pct, normalized_eps_cagr_5y_pct, "
            "latest_roce_or_roe_pct, net_debt_to_equity, cfo_pat_ratio, "
            "liquidity_value_inr_cr")
    a = pd.DataFrame(db.fetch_all(
        f"SELECT {cols} FROM market.phase1_universe WHERE run_id = %s", (base,)))
    b = pd.DataFrame(db.fetch_all(
        f"SELECT {cols} FROM market.phase1_universe WHERE run_id = %s", (other,)))
    if a.empty or b.empty:
        return {"error": "one or both runs have no universe rows"}

    ca = {r["symbol"] for r in db.fetch_all("""
        SELECT u.symbol FROM market.phase1_candidate c
        JOIN market.phase1_universe u USING (run_id, security_id)
        WHERE c.run_id = %s""", (base,))}
    cb = {r["symbol"] for r in db.fetch_all("""
        SELECT u.symbol FROM market.phase1_candidate c
        JOIN market.phase1_universe u USING (run_id, security_id)
        WHERE c.run_id = %s""", (other,))}

    m = a.merge(b, on="symbol", suffixes=("_a", "_b"), how="outer", indicator=True)
    only_a = m[m["_merge"] == "left_only"]["symbol"].tolist()
    only_b = m[m["_merge"] == "right_only"]["symbol"].tolist()
    both = m[m["_merge"] == "both"]

    changed: dict[str, int] = {}
    examples: dict[str, list] = {}
    for col in a.columns:
        if col == "symbol" or col in _NOISE_COLS:
            continue
        x, y = both[f"{col}_a"], both[f"{col}_b"]
        if pd.api.types.is_numeric_dtype(x) or pd.api.types.is_numeric_dtype(y):
            xx = pd.to_numeric(x, errors="coerce")
            yy = pd.to_numeric(y, errors="coerce")
            d = ~((xx - yy).abs() <= tolerance * xx.abs().clip(lower=1)) & \
                ~(xx.isna() & yy.isna())
        else:
            d = x.fillna("") != y.fillna("")
        if d.any():
            changed[col] = int(d.sum())
            examples[col] = [
                {"symbol": r.symbol, "from": getattr(r, f"{col}_a"),
                 "to": getattr(r, f"{col}_b")}
                for r in both[d].head(3).itertuples(index=False)]

    def _el(df, suffix):
        return set(df[df[f"eligible_flag{suffix}"] == 1]["symbol"])

    el_a, el_b = _el(both, "_a"), _el(both, "_b")

    return {
        "base": base, "other": other,
        "universe": {"only_in_base": len(only_a), "only_in_other": len(only_b),
                     "in_both": len(both)},
        "eligibility": {"base": len(el_a), "other": len(el_b),
                        "gained": sorted(el_b - el_a)[:20],
                        "lost": sorted(el_a - el_b)[:20],
                        "gained_count": len(el_b - el_a),
                        "lost_count": len(el_a - el_b)},
        "candidates": {"base": len(ca), "other": len(cb),
                       "entered": sorted(cb - ca)[:25],
                       "left": sorted(ca - cb)[:25],
                       "entered_count": len(cb - ca), "left_count": len(ca - cb),
                       "unchanged": len(ca & cb)},
        "changed_fields": changed,
        "examples": examples,
    }


def prune_runs(db: Database, keep: int = 10) -> dict:
    """Delete all but the most recent `keep` runs. Cascades to the child tables."""
    doomed = db.fetch_all("""
        SELECT run_id FROM market.screen_run
        ORDER BY started_at DESC OFFSET %s
    """, (keep,))
    if not doomed:
        return {"deleted": 0}
    ids = [r["run_id"] for r in doomed]
    db.execute("DELETE FROM market.screen_run WHERE run_id = ANY(%s)", (ids,))
    return {"deleted": len(ids), "kept": keep}
