"""
Phase 2 verdicts: combining forensic and valuation evidence, and narrowing.

Two rules shape this.

**A disqualifying forensic finding is not tradeable against a cheap price.** It
is applied as a gate, before any scoring, for the same reason the Weinstein stage
became a gate in Phase 1 (D8): a weighted component with enough other points
around it stops deciding anything. A company whose cumulative CFO covers 40% of
its reported profit should not advance because its P/E is low - that combination
is what a value trap looks like from the outside.

**The count is a signal.** Same discipline as the Phase 1 score floor (D9):
everything clearing the bar, capped at the target, and the run records which
constraint bound. If only 22 of 150 survive, the answer is 22, not a padded 40.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import forensics, valuation

# The brief's Phase 2 output band.
TARGET_LOW, TARGET_HIGH = 40, 60

# The verdict comes from this matrix, not from a threshold on the combined
# score. That was the first design and it failed the same way Phase 1's score
# floor did: three already-high inputs average into a compressed range - min
# 53, median 74 - so any bar low enough to look reasonable never bound, and 122
# of 150 "advanced". The narrowing was being done entirely by the cap.
#
# The rule an analyst actually applies is a joint one. You will accept forensic
# questions if the price pays you for them, and you will accept a full price if
# the statements are clean; you will not accept both at once. Expressed
# directly, the evidence narrows the set and the score only ranks within it -
# the same lesson as the Weinstein gate in D8.
VERDICT_MATRIX = {
    #  severity        cheap      fair       full       stretched
    "clean":        ("advance", "advance", "advance", "hold"),
    "watch":        ("advance", "advance", "hold",    "hold"),
    "concern":      ("advance", "hold",    "reject",  "reject"),
    "disqualifying": ("reject", "reject",  "reject",  "reject"),
}
_VAL_ORDER = ("cheap", "fair", "full", "stretched")

# Forensic and valuation weights, used only to RANK the advancing set. Forensic
# carries more because a valuation is an opinion about a number, whereas a
# forensic flag questions whether the number means anything.
W_FORENSIC, W_VALUATION, W_PHASE1 = 0.45, 0.30, 0.25


@dataclass(frozen=True)
class Verdict:
    verdict: str            # advance | hold | reject
    reason: str
    combined_score: float | None


def open_questions(archetype: str | None, flags: list[forensics.Flag]) -> str:
    """
    What this assessment could not answer, and why.

    Phase 1 hands each archetype a set of research questions, and most of them
    need a filing this system has never fetched. Listing them keeps the gap
    visible instead of letting a clean forensic score read as a clean company.
    """
    items = [
        "auditor qualifications and emphases of matter",
        "related-party transactions",
        "promoter share pledging",
        "contingent liabilities",
    ]
    codes = {f.code for f in flags}
    if "CWIP_STALLED" in codes:
        items.append("CWIP ageing and commissioning dates")
    if "RECEIVABLES_OUTPACING_SALES" in codes or "INVENTORY_BUILD" in codes:
        items.append("receivable ageing and provisioning policy")
    if archetype == "Capex operating-leverage candidate":
        items.append("capex vs original budget, and demand contracts for new capacity")
    if archetype == "Financial compounder":
        items.append("GNPA/NNPA, provision coverage and credit-cost trend")
    if archetype == "Event-driven or special situation":
        items.append("scheme structure, record date and regulatory path")
    return "; ".join(items) + " - all require the annual report or filings"


def decide(forensic_score: float, severity: str,
           valuation_score: float | None, valuation_verdict: str,
           phase1_score: float | None,
           methods: list[valuation.Method]) -> Verdict:
    """One company's verdict, on the evidence assembled."""
    if severity == "disqualifying":
        return Verdict("reject",
                       "a disqualifying forensic finding is not offset by price",
                       None)

    # A valuation nothing could assess is a reason to hold, not to advance. It
    # usually means no positive earnings, which Phase 3 cannot price either.
    if valuation_verdict == "unassessable":
        return Verdict("hold",
                       "no valuation method could be applied - typically no "
                       "positive earnings or book value",
                       None)

    p1 = phase1_score if phase1_score is not None else 60.0
    combined = round(W_FORENSIC * forensic_score
                     + W_VALUATION * (valuation_score or 0.0)
                     + W_PHASE1 * p1, 2)

    outcome = VERDICT_MATRIX[severity][_VAL_ORDER.index(valuation_verdict)]

    if outcome == "advance":
        reason = (f"statements {severity}, priced {valuation_verdict}")
        if valuation.disagreement(methods):
            reason += " (methods disagree - read the valuation detail)"
    elif outcome == "hold":
        reason = (f"statements {severity} against a {valuation_verdict} price - "
                  f"needs a filing before this can advance")
    else:
        reason = (f"forensic {severity} at a {valuation_verdict} price leaves "
                  f"nothing to be paid for the risk")
    return Verdict(outcome, reason, combined)


def select(rows: list[dict], target_high: int = TARGET_HIGH) -> tuple[list[dict], str]:
    """
    The advancing set, ranked, capped at the target.

    Returns (selected, bound_by). `bound_by == "evidence"` means the market did
    not offer a full slate at this bar - which is information, not a failure.
    """
    advancing = sorted(
        [r for r in rows if r["verdict"] == "advance"],
        key=lambda r: (-(r["combined_score"] or 0), r["symbol"]))
    if len(advancing) > target_high:
        return advancing[:target_high], "target"
    return advancing, "evidence"
