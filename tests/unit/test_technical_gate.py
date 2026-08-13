"""
The technical gate: stage exclusion as a hard eligibility rule.

The failure mode worth guarding is not a wrong verdict, it is a gate that does
nothing. A mistyped stage name matches no company, every run looks healthy, and
the filter the operator configured is simply absent - the same shape as a QC
check that cannot fail. Hence the validation tests here and QC19 in the pipeline.
"""

from __future__ import annotations

import dataclasses

import pytest

from market_screener.config import ScreenSettings
from market_screener.domain import eligibility
from market_screener.domain.eligibility import TechnicalGate
from market_screener.domain.weinstein import STAGES

OK_METRICS = {"company": "Test Ltd", "fy_count": 5}
DEFAULT_STAGES = ("Stage 3 distribution", "Stage 4 decline")


def eligible_args(**over):
    """Arguments that clear every gate before the technical one."""
    base = dict(metrics=OK_METRICS, market_cap=5000.0, weeks_history=200,
                liquidity_inr_cr=10.0)
    base.update(over)
    return base


# ---- construction and validation -------------------------------------------

def test_unknown_stage_name_is_rejected():
    with pytest.raises(ValueError, match="unknown Weinstein stage"):
        TechnicalGate(exclude_stages=frozenset({"Stage 4 Decline"}))  # wrong case


def test_every_default_stage_is_a_real_stage():
    """The shipped default must match the analyser's vocabulary exactly."""
    assert set(DEFAULT_STAGES) <= set(STAGES)
    assert set(ScreenSettings().technical_gate_exclude_stages) <= set(STAGES)


def test_empty_gate_is_inactive():
    assert not TechnicalGate().active
    assert TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES)).active
    assert TechnicalGate(min_rs_13w_pct=0.0).active


# ---- the gate in isolation --------------------------------------------------

@pytest.mark.parametrize("stage,expected", [
    ("Stage 4 decline", False),
    ("Stage 3 distribution", False),
    ("Early Stage 2", True),
    ("Mature/extended Stage 2", True),
    ("Early Stage 1", True),
    ("Mature Stage 1 base", True),
    ("Stage 1-to-Stage 2 transition", True),
])
def test_default_gate_verdict_by_stage(stage, expected):
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES))
    assert g.assess({"technical_stage": stage}).eligible is expected


def test_excluded_stage_reports_the_stage_in_the_reason():
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES))
    v = g.assess({"technical_stage": "Stage 4 decline"})
    assert v.code == "EX_TECHNICAL_STAGE"
    assert "Stage 4 decline" in v.reason


def test_indeterminate_is_excluded_by_default():
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES))
    v = g.assess({"technical_stage": eligibility.INDETERMINATE})
    assert not v.eligible and v.code == "EX_NO_TECHNICAL_READ"


def test_indeterminate_can_be_allowed_through():
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES),
                      exclude_indeterminate=False)
    assert g.assess({"technical_stage": eligibility.INDETERMINATE}).eligible


def test_missing_stage_is_treated_as_indeterminate():
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES))
    assert not g.assess({}).eligible


# ---- the relative-strength floor -------------------------------------------

def test_rs_floor_rejects_below_and_admits_at_the_boundary():
    g = TechnicalGate(min_rs_13w_pct=0.0)
    assert not g.assess({"technical_stage": "Early Stage 2",
                         "rs_bm_13w_pct": -0.1}).eligible
    assert g.assess({"technical_stage": "Early Stage 2",
                     "rs_bm_13w_pct": 0.0}).eligible


def test_missing_rs_fails_a_configured_floor():
    """Absent evidence is not evidence of strength."""
    g = TechnicalGate(min_rs_13w_pct=0.0)
    v = g.assess({"technical_stage": "Early Stage 2"})
    assert not v.eligible and v.code == "EX_WEAK_RS"


def test_rs_is_not_consulted_when_no_floor_is_set():
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES))
    assert g.assess({"technical_stage": "Early Stage 2",
                     "rs_bm_13w_pct": -50.0}).eligible


# ---- integration with assess() ---------------------------------------------

def test_assess_without_a_gate_is_the_oracle_behaviour():
    """The four-argument form must stay identical to the frozen oracle."""
    v = eligibility.assess(**eligible_args())
    assert v.eligible
    v = eligibility.assess(**eligible_args(),
                           tech={"technical_stage": "Stage 4 decline"})
    assert v.eligible, "a tech dict alone must not gate; a gate must be passed"


def test_assess_applies_the_gate_when_given_one():
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES))
    v = eligibility.assess(**eligible_args(),
                           tech={"technical_stage": "Stage 4 decline"}, gate=g)
    assert not v.eligible and v.code == "EX_TECHNICAL_STAGE"


def test_gate_runs_last_so_existing_exclusion_labels_do_not_move():
    """
    An illiquid Stage 4 company must still be labelled EX_ILLIQUID. Gates
    short-circuit on the first failure, so inserting the technical gate earlier
    would silently relabel exclusions whose meaning has not changed.
    """
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES))
    v = eligibility.assess(**eligible_args(liquidity_inr_cr=0.1),
                           tech={"technical_stage": "Stage 4 decline"}, gate=g)
    assert v.code == "EX_ILLIQUID"


def test_gate_does_not_rescue_a_company_failing_an_earlier_gate():
    g = TechnicalGate(exclude_stages=frozenset(DEFAULT_STAGES))
    v = eligibility.assess(**eligible_args(market_cap=10.0),
                           tech={"technical_stage": "Early Stage 2"}, gate=g)
    assert v.code == "EX_MCAP_BELOW_BAND"


# ---- config plumbing --------------------------------------------------------

def test_from_settings_reads_the_shipped_defaults():
    g = TechnicalGate.from_settings(ScreenSettings())
    assert g.active
    assert g.exclude_stages == frozenset(DEFAULT_STAGES)


def test_from_settings_honours_an_empty_stage_tuple():
    s = dataclasses.replace(ScreenSettings(), technical_gate_exclude_stages=())
    assert not TechnicalGate.from_settings(s).active


def test_gate_changes_the_config_hash():
    """
    Two runs on different gates must be distinguishable, or the parity suite
    cannot pin to an ungated run and `runs diff` cannot explain the delta.
    """
    from market_screener.config import (HttpSettings, PostgresSettings, Settings)
    from pathlib import Path

    def mk(stages):
        return Settings(
            project_root=Path("."), domain="operational",
            paths=None, pg=PostgresSettings(), http=HttpSettings(),
            screen=dataclasses.replace(ScreenSettings(),
                                       technical_gate_exclude_stages=stages))

    assert mk(()).config_hash() != mk(DEFAULT_STAGES).config_hash()


def test_every_new_exclusion_code_is_documented():
    for code in ("EX_TECHNICAL_STAGE", "EX_WEAK_RS", "EX_NO_TECHNICAL_READ"):
        assert eligibility.EXCLUSION_CODES.get(code)
