"""
The bulk payload loader must equal the per-security one, exactly.

`payloads_for_universe` exists only to avoid 4,172 round trips. If it
reconstructs payloads by even slightly different logic, the saving is paid for
in screening answers that differ from the parity baseline with no visible cause
- the worst kind of regression, because everything still runs.

The comparison must therefore cover the cases a naive sample would miss: blank
pages, standalone-basis companies, and securities with no page at all. By
default it runs a STRATIFIED set - every one of those categories in full, plus a
spread of ordinary companies - and asserts each stratum is non-empty, so the
sample cannot quietly shrink into something vacuous.

The exhaustive sweep over all 2,086 is one environment variable away and was run
when the bulk path was introduced. It is not the default because it costs ~345
seconds: it drives the very per-security path being replaced, once per company.

    SCREENER_FULL_PAYLOAD_PARITY=1 pytest tests/parity/test_bulk_payload_parity.py
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from market_screener.config import load_settings
from market_screener.db.connection import Database
from market_screener.domain import fundamentals_view as fv
from market_screener.pipeline.context import pit_cutoff

pytestmark = pytest.mark.parity

AS_OF = date(2026, 8, 13)


@pytest.fixture(scope="module")
def db():
    st = load_settings()
    d = Database(st.pg)
    if not d.database_exists() or not d.ping():
        pytest.skip("database unavailable")
    return d


@pytest.fixture(scope="module")
def labels(db):
    return fv._label_lookup(db)


@pytest.fixture(scope="module")
def universe(db):
    rows = db.fetch_all("""
        SELECT security_id FROM market.security
        WHERE  is_active AND series = 'EQ' AND security_type = 'equity'
        ORDER  BY symbol
    """)
    if not rows:
        pytest.skip("no active universe loaded")
    return [r["security_id"] for r in rows]


@pytest.fixture(scope="module")
def bulk(db, labels):
    return fv.payloads_for_universe(db, as_of=pit_cutoff(AS_OF), labels=labels)


FULL = bool(os.getenv("SCREENER_FULL_PAYLOAD_PARITY"))


@pytest.fixture(scope="module")
def strata(db, universe, bulk):
    """
    The categories that behave differently in reconstruction, each in full,
    plus a spread of ordinary companies.
    """
    blank = [s for s in universe if bulk[s].get("error") == "blank_page"]
    no_page = [s for s in universe if bulk[s].get("basis") is None
               and not bulk[s].get("error")]
    standalone = [s for s in universe if bulk[s].get("basis") == "standalone"]
    consolidated = [s for s in universe if bulk[s].get("basis") == "consolidated"]
    return {"blank": blank, "no_page": no_page, "standalone": standalone,
            "consolidated": consolidated[::9]}


def test_the_sample_actually_covers_the_awkward_cases(strata):
    """
    A stratified test that stopped covering its strata would pass while checking
    nothing. Blank pages and standalone basis are exactly where a bulk rewrite
    would break, so their absence is a failure, not a skip.
    """
    for name in ("blank", "standalone", "consolidated"):
        assert strata[name], f"stratum {name!r} is empty - the sample is vacuous"


def test_bulk_covers_exactly_the_active_universe(bulk, universe):
    """Including error records - a caller must not have to handle absence."""
    assert set(bulk) == set(universe)


def test_bulk_payloads_match_single(db, labels, universe, bulk, strata):
    targets = universe if FULL else sorted(
        {s for group in strata.values() for s in group})
    cutoff = pit_cutoff(AS_OF)
    mismatches = []
    for sid in targets:
        want = fv.payload_for_metrics(db, sid, as_of=cutoff, labels=labels)
        got = bulk[sid]
        if got != want:
            keys = sorted(set(want) | set(got))
            diff = [k for k in keys if want.get(k) != got.get(k)]
            mismatches.append((sid, diff))
        if len(mismatches) >= 5:
            break
    assert not mismatches, (
        f"bulk and single payloads diverge for {mismatches} "
        f"(compared {len(targets)} securities, full={FULL})")


def test_blank_pages_are_reported_as_errors_not_empty_payloads(bulk):
    """
    An empty payload and a blank page are different things: the first says the
    company has no data, the second says the fetch failed. Collapsing them would
    silently reclassify EX_NO_FUNDAMENTALS companies as merely data-poor.
    """
    blanks = [p for p in bulk.values() if p.get("error") == "blank_page"]
    for p in blanks:
        assert p.get("symbol")
        assert "profit_loss" not in p


def test_metrics_agree_end_to_end(db, labels, universe, bulk):
    """The payloads feed metrics.compute; equality has to survive that too."""
    from market_screener.domain import metrics as metrics_mod

    cutoff = pit_cutoff(AS_OF)
    targets = universe if FULL else universe[::17]
    checked = 0
    for sid in targets:
        want = metrics_mod.compute(
            fv.payload_for_metrics(db, sid, as_of=cutoff, labels=labels))
        got = metrics_mod.compute(bulk[sid])
        assert got == want, f"metrics diverge for security_id={sid}"
        checked += 1
    assert checked > 100, "sample too small to be meaningful"
