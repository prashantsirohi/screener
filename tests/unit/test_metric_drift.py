"""
Metric-label drift detection.

metric_id is a slug of the aggregator's display label, so a renamed row mints a
new id and the old one silently stops receiving facts. Nothing errors; ratios
built on the old id just become None everywhere, which is indistinguishable from
companies that do not report it.

The point of these tests is that the detector FIRES. Two checks have already
shipped in this codebase that could not fail (QC05/QC09 queried a table nothing
wrote), so every branch here is driven with fabricated drift.
"""

from __future__ import annotations

import pytest

from market_screener.domain import metric_map as mm
from market_screener.ingest import metric_drift as md

V = "mapping-v1"


def row(mid, label, securities, unit="inr_cr", statement="profit_loss",
        version=V):
    return {"metric_id": mid, "statement": statement, "metric_label": label,
            "unit": unit, "mapping_version": version,
            "securities": securities, "facts": securities * 10}


BASE = [
    row("profit_loss.sales", "Sales", 2000),
    row("profit_loss.net_profit", "Net Profit", 2000),
    row("ratios.roce_pct", "ROCE %", 1900, unit="pct", statement="ratios"),
]


# ---- the four alert types ---------------------------------------------------

def test_no_drift_is_reported_as_no_drift():
    rep = md.compare(BASE, BASE)
    assert rep["status"] == "ok"
    assert not md.has_findings(rep)


def test_a_vanished_metric_is_detected():
    after = [r for r in BASE if r["metric_id"] != "ratios.roce_pct"]
    rep = md.compare(BASE, after)
    assert [r["metric_id"] for r in rep["vanished"]] == ["ratios.roce_pct"]
    assert md.has_findings(rep)


def test_a_new_label_is_detected():
    after = BASE + [row("profit_loss.other_income", "Other Income", 1500)]
    rep = md.compare(BASE, after)
    assert [r["metric_id"] for r in rep["appeared"]] == ["profit_loss.other_income"]


def test_a_unit_change_is_detected():
    after = [row("profit_loss.sales", "Sales", 2000, unit="inr"),
             *BASE[1:]]
    rep = md.compare(BASE, after)
    assert rep["unit_changed"] == [
        {"metric_id": "profit_loss.sales", "was": "inr_cr", "now": "inr",
         "label": "Sales"}]


def test_a_coverage_collapse_is_detected():
    after = [row("profit_loss.sales", "Sales", 400), *BASE[1:]]
    rep = md.compare(BASE, after)
    assert len(rep["coverage_drop"]) == 1
    assert rep["coverage_drop"][0]["drop_pct"] == 80.0


# ---- the noise floor --------------------------------------------------------

def test_small_coverage_moves_are_not_reported():
    """Routine growth and the odd recovered page must stay quiet."""
    after = [row("profit_loss.sales", "Sales", 1900), *BASE[1:]]
    assert not md.compare(BASE, after)["coverage_drop"]


def test_rare_metrics_do_not_trigger_percentage_alarms():
    """A metric held by 4 companies losing one is a 25% 'collapse' meaning nothing."""
    before = BASE + [row("profit_loss.exceptional", "Exceptional Items", 4)]
    after = BASE + [row("profit_loss.exceptional", "Exceptional Items", 3)]
    assert not md.compare(before, after)["coverage_drop"]


def test_coverage_growth_is_not_a_drop():
    after = [row("profit_loss.sales", "Sales", 2500), *BASE[1:]]
    assert not md.compare(BASE, after)["coverage_drop"]


# ---- renames ----------------------------------------------------------------

def test_a_rename_is_paired_rather_than_reported_twice():
    """
    The real-world case: 'Sales' becomes 'Revenue' and coverage is unchanged.
    Reporting one vanished and one appeared is technically true and useless.
    """
    after = [row("profit_loss.revenue", "Revenue", 2000), *BASE[1:]]
    rep = md.compare(BASE, after)
    assert len(rep["likely_renames"]) == 1
    r = rep["likely_renames"][0]
    assert r["from_label"] == "Sales" and r["to_label"] == "Revenue"


def test_unrelated_appear_and_vanish_are_not_paired_as_a_rename():
    """Different statements, and coverage nowhere near each other."""
    after = [r for r in BASE if r["metric_id"] != "profit_loss.sales"] + [
        row("ratios.debtor_days", "Debtor Days", 50, unit="days",
            statement="ratios")]
    assert not md.compare(BASE, after)["likely_renames"]


# ---- telling their change from ours -----------------------------------------

def test_our_own_mapping_change_is_flagged_as_such():
    """
    Editing LABEL_ALIASES looks identical to the source renaming a row unless
    the mapping version is compared too.
    """
    after = [row("profit_loss.revenue", "Revenue", 2000, version="mapping-v2"),
             *[dict(r, mapping_version="mapping-v2") for r in BASE[1:]]]
    assert md.compare(BASE, after)["mapping_changed_by_us"] is True


def test_source_side_change_is_not_blamed_on_us():
    after = [row("profit_loss.revenue", "Revenue", 2000), *BASE[1:]]
    assert md.compare(BASE, after)["mapping_changed_by_us"] is False


# ---- the mapping version itself ---------------------------------------------

def test_mapping_version_is_stable_across_calls():
    assert mm.mapping_version() == mm.mapping_version()


def test_mapping_version_tracks_the_alias_table(monkeypatch):
    before = mm.mapping_version()
    monkeypatch.setitem(mm.LABEL_ALIASES, "revenue", "sales")
    assert mm.mapping_version() != before


def test_mapping_version_tracks_the_unit_table(monkeypatch):
    before = mm.mapping_version()
    monkeypatch.setitem(mm.UNIT_OVERRIDES, "profit_loss.sales", "inr")
    assert mm.mapping_version() != before


def test_mapping_version_ignores_unrelated_module_edits():
    """
    It fingerprints the mapping DATA, not the source file - otherwise a comment
    change would invalidate it and the signal would be worthless.
    """
    import inspect
    src = inspect.getsource(mm.mapping_version)
    assert "LABEL_ALIASES" in src and "UNIT_OVERRIDES" in src
    assert "getsource" not in src


def test_insufficient_history_is_reported_not_faked():
    assert md.has_findings({"status": "insufficient_history", "snapshots": 1}) is False
