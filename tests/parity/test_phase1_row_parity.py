"""
Phase 1 row parity against the frozen baseline.

The three defect fixes deliberately changed outputs, so "all 37 columns identical"
is the wrong bar - it would only be met by not fixing the bugs. What must hold is
that every difference is ATTRIBUTABLE:

* **Recovered companies.** 306 whose fundamentals page returned a blank shell now
  have data. They were excluded as EX_NO_MCAP before and are screened now.
* **Technical window.** Excluding the current partial week moves the last bar, so
  moving averages, relative strength, the 52-week high and the 13-week liquidity
  median all shift slightly - and the priority score with them.
* **Source IDs.** Minted from real ingest provenance instead of hardcoded.

Anything else is a regression. The strongest assertion here is that the
fundamental columns are byte-identical for every company whose underlying data
did not change - those depend on neither the price window nor the retry queue.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from market_screener.config import load_settings
from market_screener.db.connection import Database

pytestmark = pytest.mark.parity

BASELINE = Path("phase1_baseline_20260810/outputs")

SCREEN_COL_COUNT = 37

# Differ by design; each is covered by its own assertion below.
ATTRIBUTABLE_COLS = {
    "primary_source_ids", "secondary_source_ids",   # provenance-derived now
    "technical_data_date",                          # ISO-Friday + complete weeks
    "price_date", "market_cap_date",
    # A later run has a later as_of; the baseline is dated 2026-08-10.
    "screening_date",
    # The company name now comes from NSE's EQUITY_L entry rather than the
    # scraped page heading - "20 Microns Limited" instead of "20 Microns Ltd".
    # The exchange's own name is the authoritative one, and the label feeds no
    # screening logic. test_company_names_are_the_same_entity pins it down.
    "company",
}

LEGAL_SUFFIXES = (" limited", " ltd", " ltd.", " private", " pvt", " corporation",
                  " corp", " company", " co", " co.", " (india)", " india")
# Move with the technical window.
WINDOW_SENSITIVE_COLS = {
    "liquidity_value_inr_cr", "liquidity_period", "technical_stage",
    "preliminary_priority_score", "secondary_tags", "inclusion_reason",
    "classification_rationale", "primary_archetype", "key_disqualifying_risk",
    "current_price_inr",
}
# Depend on neither the price window nor the retry queue.
FUNDAMENTAL_COLS = [
    "revenue_cagr_5y_pct", "normalized_eps_cagr_5y_pct", "latest_roce_or_roe_pct",
    "median_roce_or_roe_5y_pct", "net_debt_to_equity", "cfo_pat_ratio",
    "cfo_pat_period", "preliminary_valuation_value",
]


@pytest.fixture(scope="module")
def db():
    st = load_settings()
    d = Database(st.pg)
    if not d.database_exists() or not d.ping():
        pytest.skip("database unavailable")
    return d


BASELINE_BASIS = "yahoo_adjclose"

# The screening config the FROZEN BASELINE was produced under. Every entry is a
# production default that has since moved; each is pinned here so this suite
# keeps comparing like with like.
#
# This suite exists to prove the PORT changed no answers. Letting it follow a
# default that has deliberately changed would conflate that with the behaviour
# change, and each of these has its own treatment in docs/decisions.md.
BASELINE_SCREEN = {
    # D8: the gate excludes whole Weinstein stages from eligibility, so a gated
    # run has entire rows the baseline screened. Not row-comparable.
    "technical_gate_exclude_stages": (),
    "technical_gate_min_rs_13w": None,
    # The floor was raised 45 -> 60. It changes no compared row - it selects
    # within phase1_universe rather than producing it - but it is in
    # config_hash, so it must be pinned for the hash to resolve.
    "min_select_score": 45.0,
}


def baseline_config_hash():
    """
    The config hash of a run this suite can legitimately compare against.

    config_hash covers the basis and everything in ScreenSettings, so
    reconstructing the baseline variant and matching on its hash selects a
    genuinely comparable run.

    Note the failure mode this guards: adding ANY field to ScreenSettings
    re-hashes every historical run, and an unpinned addition leaves the fixture
    matching nothing. The suite would then skip - silently retiring the
    acceptance gate at exactly the moment screening behaviour changed. The
    skip message below is the visible signal; test_baseline_run_exists is the
    loud one.
    """
    import dataclasses

    from market_screener.config import load_settings

    st = load_settings()
    return dataclasses.replace(
        st, price_basis=BASELINE_BASIS,
        screen=dataclasses.replace(st.screen, **BASELINE_SCREEN)).config_hash()


def test_baseline_run_exists(db):
    """
    Fail loudly, rather than skipping, when nothing is comparable.

    Every other test here depends on a fixture that SKIPS when the baseline-config
    run is absent, and a suite of skips reads as green. That has now nearly
    happened twice - adding the technical gate and then the score floor each
    re-hashed every historical run. This test is the alarm.
    """
    want = baseline_config_hash()
    n = db.fetch_value("""
        SELECT count(*) AS c FROM market.screen_run
        WHERE  phase = 1 AND status = 'complete' AND config_hash = %s
    """, (want,))
    assert n, (
        f"no completed Phase 1 run with the frozen baseline's config "
        f"(hash {want}: basis={BASELINE_BASIS}, {BASELINE_SCREEN}). The row "
        f"parity tests cannot run. If a ScreenSettings field was just added, "
        f"pin it in BASELINE_SCREEN; otherwise regenerate the run.")


@pytest.fixture(scope="module")
def latest_run(db):
    want = baseline_config_hash()
    rid = db.fetch_value("""
        SELECT run_id FROM market.screen_run
        WHERE  phase = 1 AND status = 'complete' AND config_hash = %s
        ORDER  BY started_at DESC LIMIT 1
    """, (want,))
    if not rid:
        pytest.skip(
            f"no completed Phase 1 run matching the frozen baseline's config "
            f"(basis={BASELINE_BASIS}, technical gate off); produce one with:\n"
            f"  SCREENER_PRICE_BASIS={BASELINE_BASIS} "
            f"SCREENER_TECHNICAL_GATE=off screener screen --force")
    return rid


@pytest.fixture(scope="module")
def current(db, latest_run):
    path = db.fetch_value("""
        SELECT path FROM market.screen_artifact
        WHERE  run_id = %s AND artifact_name = 'P1_screened_universe.csv'
    """, (latest_run,))
    if not path or not Path(path).exists():
        pytest.skip("run artifacts missing")
    return pd.read_csv(path)


@pytest.fixture(scope="module")
def baseline():
    p = BASELINE / "P1_screened_universe.csv"
    if not p.exists():
        pytest.skip("frozen baseline not present")
    return pd.read_csv(p)


@pytest.fixture(scope="module")
def recovered(db):
    rows = db.fetch_all(
        "SELECT scope FROM market.fetch_retry_queue "
        "WHERE source = 'fundamentals.screener' AND state = 'resolved'")
    return {r["scope"] for r in rows}


# ---------------- contract ----------------

def test_column_contract_is_exact(current, baseline):
    assert list(current.columns) == list(baseline.columns), \
        "the 37-column contract must not drift"
    assert len(current.columns) == SCREEN_COL_COUNT


def test_universe_is_the_same_size(current, baseline):
    assert len(current) == len(baseline) == 2086


def test_same_symbols_screened(current, baseline):
    assert set(current["symbol"]) == set(baseline["symbol"])


# ---------------- attributable differences ----------------

def test_every_newly_eligible_company_is_explained(current, baseline, recovered):
    """
    A company that was excluded before and is eligible now must be one the retry
    queue recovered. Anything else means the eligibility gates moved.
    """
    m = baseline.merge(current, on="symbol", suffixes=("_base", "_now"))
    newly = m[(m["eligible_flag_base"] == 0) & (m["eligible_flag_now"] == 1)]
    unexplained = newly[~newly["symbol"].isin(recovered)]

    # A handful may also flip because the technical window moved them across the
    # 40-bar or liquidity threshold; those must have been excluded for exactly
    # those reasons before.
    window_codes = {"EX_NO_PRICE_HISTORY", "EX_ILLIQUID", "EX_NO_ARCHETYPE"}
    truly_odd = unexplained[~unexplained["exclusion_code_base"].isin(window_codes)]
    assert truly_odd.empty, (
        f"{len(truly_odd)} companies became eligible for no attributable reason: "
        f"{truly_odd[['symbol', 'exclusion_code_base']].head(10).to_dict('records')}")


def test_no_company_silently_lost_eligibility(current, baseline, recovered):
    m = baseline.merge(current, on="symbol", suffixes=("_base", "_now"))
    lost = m[(m["eligible_flag_base"] == 1) & (m["eligible_flag_now"] == 0)]
    window_codes = {"EX_NO_PRICE_HISTORY", "EX_ILLIQUID", "EX_NO_ARCHETYPE",
                    "EX_MCAP_BELOW_BAND", "EX_MCAP_ABOVE_BAND"}
    odd = lost[~lost["exclusion_code_now"].isin(window_codes)]
    assert odd.empty, (
        f"{len(odd)} companies lost eligibility unexplainably: "
        f"{odd[['symbol', 'exclusion_code_now']].head(10).to_dict('records')}")


def test_eligible_count_grew_by_roughly_the_recovered_set(current, baseline, recovered):
    base_el = int((baseline["eligible_flag"] == 1).sum())
    now_el = int((current["eligible_flag"] == 1).sum())
    assert now_el > base_el, "recovering 306 blank pages should widen the universe"
    assert now_el - base_el <= len(recovered) + 60, (
        f"eligible grew {base_el} -> {now_el}, more than the {len(recovered)} "
        f"recovered companies plus window effects can explain")


# ---------------- the strong assertion ----------------

def test_fundamentals_are_unchanged_for_untouched_companies(current, baseline,
                                                            recovered):
    """
    For a company whose page was never re-fetched, every fundamental column must
    be byte-identical. These depend on neither the price window nor the retry
    queue, so a difference here is a genuine regression in the fundamentals path.
    """
    m = baseline.merge(current, on="symbol", suffixes=("_base", "_now"))
    untouched = m[~m["symbol"].isin(recovered)]
    assert len(untouched) > 1500

    failures = []
    for col in FUNDAMENTAL_COLS:
        b, n = untouched[f"{col}_base"], untouched[f"{col}_now"]
        if pd.api.types.is_numeric_dtype(b) or pd.api.types.is_numeric_dtype(n):
            bb = pd.to_numeric(b, errors="coerce")
            nn = pd.to_numeric(n, errors="coerce")
            diff = ~((bb - nn).abs() <= 1e-6 * bb.abs().clip(lower=1)) & \
                   ~(bb.isna() & nn.isna())
        else:
            diff = (b.fillna("") != n.fillna(""))
        if diff.any():
            sample = untouched.loc[diff, ["symbol", f"{col}_base", f"{col}_now"]]
            failures.append(f"{col}: {int(diff.sum())} rows, e.g. "
                            f"{sample.head(3).to_dict('records')}")
    assert not failures, "fundamental columns changed:\n  " + "\n  ".join(failures)


def test_market_cap_unchanged_for_untouched_companies(current, baseline, recovered):
    m = baseline.merge(current, on="symbol", suffixes=("_base", "_now"))
    untouched = m[~m["symbol"].isin(recovered)]
    b = pd.to_numeric(untouched["market_cap_inr_cr_base"], errors="coerce")
    n = pd.to_numeric(untouched["market_cap_inr_cr_now"], errors="coerce")
    diff = ~((b - n).abs() <= 1e-6 * b.abs().clip(lower=1)) & ~(b.isna() & n.isna())
    assert not diff.any(), (
        f"{int(diff.sum())} untouched companies changed market cap: "
        f"{untouched.loc[diff, ['symbol']].head(5).to_dict('records')}")


def test_company_names_are_the_same_entity(current, baseline):
    """
    The name source changed, so the strings differ - but they must still name the
    same company. Compared after stripping legal suffixes and punctuation, which
    is exactly the variation switching to NSE's official name introduces.
    """
    def norm(s: str) -> str:
        t = " ".join(str(s or "").lower().split())
        # The two variations the name-source switch actually introduces: the
        # exchange spells out "and" where the aggregator uses "&", and keeps a
        # leading definite article ("The Andhra Sugars Limited").
        t = t.replace("&", " and ")
        if t.startswith("the "):
            t = t[4:]
        changed = True
        while changed:
            changed = False
            for suf in LEGAL_SUFFIXES:
                if t.endswith(suf):
                    t, changed = t[: -len(suf)].strip(" .,-"), True
        return "".join(ch for ch in t if ch.isalnum())

    m = baseline.merge(current, on="symbol", suffixes=("_base", "_now"))
    mismatched = m[m["company_base"].map(norm) != m["company_now"].map(norm)]
    # A handful of genuinely different registrations are tolerable; a wholesale
    # divergence would mean the symbol join is wrong.
    assert len(mismatched) / len(m) < 0.05, (
        f"{len(mismatched)}/{len(m)} names do not resolve to the same entity: "
        f"{mismatched[['symbol', 'company_base', 'company_now']].head(8).to_dict('records')}")


def test_differences_are_confined_to_attributable_columns(current, baseline,
                                                          recovered):
    """No column outside the attributable set may change for an untouched,
    still-eligible company."""
    m = baseline.merge(current, on="symbol", suffixes=("_base", "_now"))
    stable = m[(~m["symbol"].isin(recovered))
               & (m["eligible_flag_base"] == 1) & (m["eligible_flag_now"] == 1)]

    unexpected = []
    for col in baseline.columns:
        if col in ATTRIBUTABLE_COLS or col in WINDOW_SENSITIVE_COLS or col == "symbol":
            continue
        b, n = stable[f"{col}_base"], stable[f"{col}_now"]
        if pd.api.types.is_numeric_dtype(b) or pd.api.types.is_numeric_dtype(n):
            bb = pd.to_numeric(b, errors="coerce")
            nn = pd.to_numeric(n, errors="coerce")
            d = ~((bb - nn).abs() <= 1e-6 * bb.abs().clip(lower=1)) & \
                ~(bb.isna() & nn.isna())
        else:
            d = (b.fillna("") != n.fillna(""))
        if d.any():
            unexpected.append(f"{col}: {int(d.sum())}/{len(stable)} rows")
    assert not unexpected, ("columns changed with no attributable cause:\n  "
                            + "\n  ".join(unexpected))


# ---------------- run record ----------------

def test_run_is_recorded_with_artifacts_and_qc(db, latest_run):
    arts = db.fetch_all(
        "SELECT artifact_name, sha256, row_count FROM market.screen_artifact "
        "WHERE run_id = %s", (latest_run,))
    names = {a["artifact_name"] for a in arts}
    assert {"P1_screened_universe.csv", "P1_candidates.csv", "P1_source_log.csv",
            "P1_summary.md", "P1_run_manifest.json"} <= names
    assert all(a["sha256"] for a in arts)

    qc = db.fetch_all(
        "SELECT check_id, passed FROM market.screen_qc_result WHERE run_id = %s",
        (latest_run,))
    assert len(qc) >= 16
    assert all(r["passed"] for r in qc), \
        f"failed checks: {[r['check_id'] for r in qc if not r['passed']]}"


def test_candidate_count_is_in_range(db, latest_run):
    n = db.fetch_value(
        "SELECT count(*) AS c FROM market.phase1_candidate WHERE run_id = %s",
        (latest_run,))
    assert 100 <= n <= 150
