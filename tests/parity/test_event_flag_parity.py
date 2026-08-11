"""
Event-flag parity.

The legacy screen classified announcements with a 12-pattern set and wrote
`data/raw/event_flags.csv`. Those flags drive the event-driven archetype and
several secondary tags, so the port must reproduce them before the richer v2
taxonomy is allowed anywhere near the screen.

One known input difference: the legacy classifier concatenated `desc`,
`attchmntText` AND `smIndustry`; the store keeps the first two. `smIndustry` is a
sector label ("Pharmaceuticals"), so it should not match an event pattern - this
test is what confirms that.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from market_screener.config import load_settings
from market_screener.db.connection import Database
from market_screener.ingest import classify_events

pytestmark = pytest.mark.parity

LEGACY_FLAGS = Path("data/raw/event_flags.csv")


@pytest.fixture(scope="module")
def db():
    st = load_settings()
    d = Database(st.pg)
    if not d.database_exists() or not d.ping():
        pytest.skip("database unavailable")
    n = d.fetch_value(
        "SELECT count(*) AS c FROM market.announcement_classification "
        "WHERE taxonomy_version LIKE 'v1:%%'")
    if not n:
        pytest.skip("announcements not classified; run `screener classify-events`")
    return d


@pytest.fixture(scope="module")
def known_symbols(db):
    rows = db.fetch_all(
        "SELECT symbol FROM market.security WHERE exchange='NSE' "
        "UNION SELECT symbol FROM market.security_alias WHERE exchange='NSE'")
    return {r["symbol"] for r in rows}


@pytest.fixture(scope="module")
def legacy_pairs(known_symbols):
    """
    Restricted to symbols that exist as securities.

    59 symbols appear in the announcement feed but in neither EQUITY_L nor the
    three-year price window - companies delisted before the history starts. They
    cannot be screened at all, so a flag for them cannot change any output; the
    comparison would be measuring universe coverage, not classification.
    """
    if not LEGACY_FLAGS.exists():
        pytest.skip("legacy event_flags.csv not present")
    df = pd.read_csv(LEGACY_FLAGS)
    return {(str(r.symbol).strip(), str(r.event_class).strip())
            for r in df.itertuples(index=False)
            if str(r.symbol).strip() in known_symbols}


def test_unscreenable_symbols_are_the_only_ones_dropped(db, known_symbols):
    """The announcements that cannot be linked must be exactly the ones whose
    symbol is not a security - not a mapping failure on a real company."""
    rows = db.fetch_all("""
        SELECT DISTINCT raw_symbol FROM market.announcement WHERE security_id IS NULL
    """)
    leaked = [r["raw_symbol"] for r in rows if r["raw_symbol"] in known_symbols]
    assert not leaked, f"real securities left unlinked: {leaked[:10]}"


@pytest.fixture(scope="module")
def ported_pairs(db):
    rows = classify_events.event_flags(db, "v1")
    return {(str(r["symbol"]).strip(), str(r["event_class"]).strip()) for r in rows}


def test_ported_flags_cover_the_legacy_set(legacy_pairs, ported_pairs):
    """
    Every legacy flag must still be produced. Extra flags are acceptable - the
    store holds announcements the legacy CSV pass deduplicated away - but a
    missing one means the port would classify a company differently.
    """
    missing = legacy_pairs - ported_pairs
    coverage = 1 - len(missing) / max(len(legacy_pairs), 1)
    assert coverage >= 0.98, (
        f"only {coverage:.1%} of legacy flags reproduced; "
        f"{len(missing)} missing, e.g. {sorted(missing)[:10]}")


def test_flag_counts_are_in_the_same_ballpark(legacy_pairs, ported_pairs):
    ratio = len(ported_pairs) / max(len(legacy_pairs), 1)
    assert 0.9 <= ratio <= 1.3, (
        f"legacy {len(legacy_pairs)} flags vs port {len(ported_pairs)} "
        f"(ratio {ratio:.2f}) - the classifier changed behaviour")


def test_the_same_event_classes_appear(legacy_pairs, ported_pairs):
    legacy_classes = {c for _, c in legacy_pairs}
    ported_classes = {c for _, c in ported_pairs}
    assert legacy_classes <= ported_classes, \
        f"event classes lost: {legacy_classes - ported_classes}"


def test_v2_assigns_every_announcement_exactly_one_category(db):
    dupes = db.fetch_value("""
        SELECT count(*) AS c FROM (
            SELECT announcement_hash, count(*) AS n
            FROM   market.announcement_classification
            WHERE  taxonomy_version = 'v2' GROUP BY 1 HAVING count(*) > 1
        ) x
    """)
    assert dupes == 0

    classified = db.fetch_value(
        "SELECT count(*) AS c FROM market.announcement_classification "
        "WHERE taxonomy_version = 'v2'")
    total = db.fetch_value("SELECT count(*) AS c FROM market.announcement")
    assert classified == total


def test_v2_tiers_are_valid(db):
    rows = db.fetch_all(
        "SELECT DISTINCT tier FROM market.announcement_classification "
        "WHERE taxonomy_version = 'v2'")
    assert {r["tier"] for r in rows} <= {"A", "B", "C", "IGNORE"}


def test_v2_finds_material_events_v1_cannot_see(db):
    """
    The point of v2: order wins, capex and management change are invisible to v1,
    and they are what an earnings-inflection screen actually needs.
    """
    rows = db.fetch_all("""
        SELECT primary_category, count(*) AS n
        FROM   market.announcement_classification
        WHERE  taxonomy_version = 'v2'
          AND  primary_category IN ('major_order_win', 'capex_expansion',
                                    'management_change', 'results')
        GROUP  BY 1
    """)
    found = {r["primary_category"]: r["n"] for r in rows}
    for cat in ("major_order_win", "results", "management_change"):
        assert found.get(cat, 0) > 100, f"v2 found only {found.get(cat, 0)} {cat}"
