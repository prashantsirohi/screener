"""
Phase 2: forensics, valuation and the verdict.

Every check is driven to FIRE and to STAY QUIET, because a forensic test that
cannot raise a flag is worse than no test - it reads as a clean bill of health.
This codebase has already shipped two QC checks that could not fail, so the bar
here is that each branch is exercised deliberately.
"""

from __future__ import annotations

import pytest

from market_screener.domain import forensics, phase2, valuation
from market_screener.domain.forensics import Flag


def payload(**blocks):
    base = {"profit_loss": {}, "balance_sheet": {}, "cash_flow": {},
            "ratios": {}, "quarters": {}, "shareholding": {},
            "top_ratios": {}, "growth": {}}
    for k, v in blocks.items():
        base[k] = v
    return base


def years(*vals, start=2021):
    return {f"Mar {start + i}": v for i, v in enumerate(vals)}


def codes(flags):
    return {f.code for f in flags}


# ---- forensics: earnings quality --------------------------------------------

def test_accruals_flag_fires_when_profit_is_not_cash():
    m = {"net_profit_latest_inr_cr": 200, "cfo_latest_inr_cr": 20,
         "total_assets_inr_cr": 1000}
    assert "ACCRUALS_HIGH" in codes(forensics.assess(payload(), m))


def test_accruals_flag_quiet_when_profit_converts():
    m = {"net_profit_latest_inr_cr": 200, "cfo_latest_inr_cr": 210,
         "total_assets_inr_cr": 1000}
    assert "ACCRUALS_HIGH" not in codes(forensics.assess(payload(), m))


def test_accruals_not_applied_to_lenders():
    """For a lender CFO swings with loan-book growth; a PAT-CFO gap is growth."""
    m = {"is_financial": True, "net_profit_latest_inr_cr": 200,
         "cfo_latest_inr_cr": -500, "total_assets_inr_cr": 1000}
    assert "ACCRUALS_HIGH" not in codes(forensics.assess(payload(), m))


@pytest.mark.parametrize("cp,severity", [
    (0.30, "disqualifying"), (0.50, "concern"), (0.70, "watch")])
def test_cash_conversion_grades_by_how_bad_it_is(cp, severity):
    m = {"cfo_pat_5y": cp, "cfo_pat_period": "FY21-FY25"}
    f = next(x for x in forensics.assess(payload(), m)
             if x.code == "CASH_CONVERSION_WEAK")
    assert f.severity == severity


def test_strong_cash_conversion_raises_nothing():
    m = {"cfo_pat_5y": 1.1, "cfo_pat_period": "FY21-FY25"}
    assert "CASH_CONVERSION_WEAK" not in codes(forensics.assess(payload(), m))


# ---- forensics: working capital ---------------------------------------------

def test_receivables_outpacing_sales_fires():
    p = payload(ratios={"Debtor Days": years(40, 45, 60, 75)},
                profit_loss={"Sales": years(100, 103, 106, 108)})
    assert "RECEIVABLES_OUTPACING_SALES" in codes(forensics.assess(p, {}))


def test_receivables_growing_with_sales_is_not_flagged():
    """Both up 80% together is scale, not a collection problem."""
    p = payload(ratios={"Debtor Days": years(40, 50, 62, 72)},
                profit_loss={"Sales": years(100, 130, 160, 180)})
    assert "RECEIVABLES_OUTPACING_SALES" not in codes(forensics.assess(p, {}))


def test_working_capital_decay_ignores_a_near_zero_base():
    """
    A cycle of 2 days going to 6 is a 200% 'deterioration' worth nothing. The
    check requires a materially positive starting cycle.
    """
    p = payload(ratios={"Cash Conversion Cycle": years(2, 3, 5, 6)})
    assert "WORKING_CAPITAL_DECAY" not in codes(forensics.assess(p, {}))


def test_working_capital_decay_fires_off_a_real_base():
    p = payload(ratios={"Cash Conversion Cycle": years(60, 75, 95, 120)})
    assert "WORKING_CAPITAL_DECAY" in codes(forensics.assess(p, {}))


def test_working_capital_checks_skip_lenders():
    p = payload(ratios={"Cash Conversion Cycle": years(60, 75, 95, 120),
                        "Debtor Days": years(40, 45, 60, 75)},
                profit_loss={"Sales": years(100, 103, 106, 108)})
    got = codes(forensics.assess(p, {"is_financial": True}))
    assert not got & {"WORKING_CAPITAL_DECAY", "RECEIVABLES_OUTPACING_SALES",
                      "INVENTORY_BUILD"}


# ---- forensics: structure and ownership -------------------------------------

def test_dilution_and_promoter_selling_fire():
    m = {"equity_capital_change_5y_pct": 70, "promoter_change_pct": -6.0}
    got = codes(forensics.assess(payload(), m))
    assert {"DILUTION", "PROMOTER_SELLING"} <= got


def test_promoter_buying_is_not_a_flag():
    assert "PROMOTER_SELLING" not in codes(
        forensics.assess(payload(), {"promoter_change_pct": 2.0}))


def test_thin_interest_cover_can_be_disqualifying():
    f = next(x for x in forensics.assess(payload(), {"interest_cover_x": 1.1})
             if x.code == "INTEREST_COVER_THIN")
    assert f.severity == "disqualifying"


def test_leverage_not_flagged_for_lenders():
    """Debt IS the business for a lender."""
    m = {"is_financial": True, "gross_debt_to_equity": 6.0}
    assert "LEVERAGE_HIGH" not in codes(forensics.assess(payload(), m))


def test_clean_company_raises_nothing():
    m = {"cfo_pat_5y": 1.2, "equity_capital_change_5y_pct": 2,
         "promoter_change_pct": 0.1, "gross_debt_to_equity": 0.2,
         "interest_cover_x": 25, "fcf_positive_years_5y": 5,
         "net_profit_latest_inr_cr": 100, "cfo_latest_inr_cr": 120,
         "total_assets_inr_cr": 900}
    assert forensics.assess(payload(), m) == []
    assert forensics.severity_of([]) == "clean"
    assert forensics.score([]) == 100.0


def test_a_data_error_produces_no_false_clean_sheet():
    """A company we could not read must not look flawless."""
    assert forensics.assess(payload(), {"data_error": "blank_page"}) == []


def test_severity_is_the_worst_flag_not_the_last():
    flags = [Flag("A", "watch", ""), Flag("B", "disqualifying", ""),
             Flag("C", "concern", "")]
    assert forensics.severity_of(flags) == "disqualifying"


def test_score_falls_as_findings_accumulate():
    one = forensics.score([Flag("A", "watch", "")])
    two = forensics.score([Flag("A", "watch", ""), Flag("B", "concern", "")])
    worst = forensics.score([Flag("C", "disqualifying", "")])
    assert one > two > worst


def test_every_flag_code_raised_is_documented():
    """The summary renders a meaning per flag; an undocumented one is a blank."""
    import inspect
    src = inspect.getsource(forensics.assess)
    raised = set(__import__("re").findall(r'add\("([A-Z_]+)"', src))
    assert raised <= set(forensics.FLAG_MEANINGS), \
        raised - set(forensics.FLAG_MEANINGS)


# ---- valuation ---------------------------------------------------------------

def test_loss_making_is_unassessable_not_expensive():
    """Reporting a loss-maker as 'stretched' would be a category error."""
    methods, score, verdict = valuation.assess({"stock_pe": None})
    assert verdict == "unassessable" and score is None


def test_all_five_methods_are_always_returned():
    """Even when unassessable - a missing row is different from a known gap."""
    methods, _, _ = valuation.assess({})
    assert len(methods) == 5
    assert {m.name for m in methods} == {
        "peg", "pb_vs_roe", "earnings_yield", "pe_percentile_5y",
        "reverse_dcf_growth"}


def test_cheap_growth_reads_cheap_on_peg():
    m = {"stock_pe": 12, "reported_eps_cagr_5y_pct": 25}
    assert next(x for x in valuation.assess(m)[0] if x.name == "peg").verdict == "cheap"


def test_expensive_no_growth_reads_stretched_on_peg():
    m = {"stock_pe": 80, "reported_eps_cagr_5y_pct": 5}
    assert next(x for x in valuation.assess(m)[0]
                if x.name == "peg").verdict == "stretched"


def test_pb_is_judged_against_the_roe_that_justifies_it():
    """3x book on 40% ROE is not the same as 3x book on 8% ROE."""
    good = {"current_price_inr": 300, "book_value_inr": 100, "roe_latest_pct": 40}
    poor = {"current_price_inr": 300, "book_value_inr": 100, "roe_latest_pct": 8}
    g = next(x for x in valuation.assess(good)[0] if x.name == "pb_vs_roe")
    p = next(x for x in valuation.assess(poor)[0] if x.name == "pb_vs_roe")
    assert g.verdict in ("cheap", "fair") and p.verdict == "stretched"


def test_pe_percentile_needs_enough_history():
    m = {"stock_pe": 20}
    short = next(x for x in valuation.assess(m, [18, 22])[0]
                 if x.name == "pe_percentile_5y")
    assert short.verdict == "unassessable"


def test_pe_percentile_places_the_current_multiple():
    m = {"stock_pe": 10}
    low = next(x for x in valuation.assess(m, [20, 25, 30, 35, 40])[0]
               if x.name == "pe_percentile_5y")
    assert low.verdict == "cheap" and low.value == 0.0


def test_reverse_dcf_compares_implied_growth_to_the_record():
    """A price implying far less growth than delivered is the cheap case."""
    m = {"stock_pe": 8, "reported_eps_cagr_5y_pct": 20}
    r = next(x for x in valuation.assess(m)[0] if x.name == "reverse_dcf_growth")
    assert r.verdict == "cheap"


def test_disagreement_is_detected():
    ms = [valuation.Method("a", 1, "cheap", ""),
          valuation.Method("b", 1, "stretched", "")]
    assert valuation.disagreement(ms)
    assert not valuation.disagreement([valuation.Method("a", 1, "fair", "")])


def test_overall_verdict_is_the_median_not_the_averaged_score():
    """
    Averaging cheap and stretched into 'fair' would report an agreement that
    does not exist.
    """
    m = {"stock_pe": 15, "reported_eps_cagr_5y_pct": 30,
         "current_price_inr": 500, "book_value_inr": 20, "roe_latest_pct": 6}
    methods, _, verdict = valuation.assess(m, [10, 12, 14, 16, 18])
    assert verdict in {mm.verdict for mm in methods if mm.verdict != "unassessable"}


# ---- the verdict --------------------------------------------------------------

def test_a_disqualifying_finding_is_not_bought_off_by_a_cheap_price():
    v = phase2.decide(50, "disqualifying", 100, "cheap", 90, [])
    assert v.verdict == "reject"


def test_clean_and_cheap_advances():
    assert phase2.decide(100, "clean", 100, "cheap", 80, []).verdict == "advance"


def test_clean_but_stretched_is_held():
    assert phase2.decide(100, "clean", 0, "stretched", 90, []).verdict == "hold"


def test_concerns_advance_only_when_the_price_pays_for_them():
    assert phase2.decide(79, "concern", 100, "cheap", 70, []).verdict == "advance"
    assert phase2.decide(79, "concern", 70, "fair", 70, []).verdict == "hold"
    assert phase2.decide(79, "concern", 35, "full", 70, []).verdict == "reject"


def test_unassessable_valuation_holds_rather_than_advancing():
    v = phase2.decide(100, "clean", None, "unassessable", 90, [])
    assert v.verdict == "hold" and v.combined_score is None


def test_the_matrix_covers_every_severity_and_verdict():
    """A missing combination would raise at run time on live data."""
    for sev in forensics.SEVERITIES:
        if sev == "clean":
            assert sev in phase2.VERDICT_MATRIX
        assert sev in phase2.VERDICT_MATRIX, sev
    for row in phase2.VERDICT_MATRIX.values():
        assert len(row) == 4
        assert set(row) <= {"advance", "hold", "reject"}


def test_selection_caps_at_the_target():
    rows = [{"verdict": "advance", "combined_score": 90 - i, "symbol": f"S{i}"}
            for i in range(100)]
    sel, bound = phase2.select(rows)
    assert len(sel) == phase2.TARGET_HIGH and bound == "target"


def test_selection_reports_a_short_list_as_evidence_bound():
    rows = [{"verdict": "advance", "combined_score": 80, "symbol": "A"},
            {"verdict": "hold", "combined_score": 90, "symbol": "B"}]
    sel, bound = phase2.select(rows)
    assert len(sel) == 1 and bound == "evidence"


def test_only_advancing_names_are_selected():
    rows = [{"verdict": "reject", "combined_score": 99, "symbol": "R"},
            {"verdict": "advance", "combined_score": 60, "symbol": "A"}]
    sel, _ = phase2.select(rows)
    assert [r["symbol"] for r in sel] == ["A"]


def test_open_questions_always_name_the_filing_gap():
    q = phase2.open_questions("Quality compounder", [])
    assert "auditor" in q and "annual report" in q


def test_open_questions_respond_to_the_flags_raised():
    q = phase2.open_questions("Capex operating-leverage candidate",
                              [Flag("CWIP_STALLED", "watch", "")])
    assert "CWIP ageing" in q and "capex vs original budget" in q
