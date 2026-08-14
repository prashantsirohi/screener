"""
Phase 1 eligibility gates.

Extracted from the inline logic in the frozen `04_phase1_screen.py`. The gates
are ordered and short-circuiting: the FIRST one a company fails is the recorded
exclusion code, so reordering them would silently relabel exclusions even though
the eligible set stayed the same.

Thresholds match the brief and the baseline exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .weinstein import STAGES

MCAP_MIN = 1000.0          # INR crore
MCAP_MAX = 100000.0
LIQ_MIN_CR = 1.0           # median daily traded value, INR crore
MIN_FY = 3                 # annual reporting periods
MIN_WEEKS_PRICE = 40       # below this the 40-week MA does not exist

EXCLUSION_CODES = {
    "EX_NO_FUNDAMENTALS": (
        "No usable financial record could be retrieved for the symbol (typically "
        "a trust, REIT/InvIT, recently suspended scrip, or a symbol with no "
        "aggregator page)."),
    "EX_NO_MCAP": "Market capitalisation not available from any source used.",
    "EX_MCAP_BELOW_BAND": "Market cap below the INR 1,000 cr floor.",
    "EX_MCAP_ABOVE_BAND": "Market cap above the INR 1,00,000 cr ceiling.",
    "EX_ILLIQUID": (
        "Median daily traded value below INR 1.0 cr over the trailing 13 weeks."),
    "EX_SHORT_FIN_HISTORY": "Fewer than 3 annual reporting periods available.",
    "EX_NO_PRICE_HISTORY": (
        "Fewer than 40 adjusted weekly bars - Weinstein staging impossible."),
    "EX_NO_ARCHETYPE": (
        "No archetype discovery test passed on the available data; the company "
        "does not present an identifiable return mechanism at Phase 1 depth."),
    "EX_DATA_QUALITY": (
        "Core screening metrics missing - classification would not be reliable."),
    "EX_TECHNICAL_STAGE": (
        "Weinstein stage is one the technical gate excludes - the business may "
        "be sound but the chart is not in an ownable stage."),
    "EX_WEAK_RS": (
        "13-week relative strength against the broad benchmark is below the "
        "configured floor."),
    "EX_NO_TECHNICAL_READ": (
        "Technical stage could not be determined, so the technical gate cannot "
        "be satisfied."),
}

INDETERMINATE = "Indeterminate-insufficient adjusted history"


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    code: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class TechnicalGate:
    """
    Turns the Weinstein stage from a scoring nudge into a hard gate.

    Stage names are validated against `weinstein.STAGES` on construction. A typo
    would otherwise match nothing and disable the gate silently, which is the
    same failure mode as a QC check that cannot fail - the run looks healthy and
    the filter is simply absent.
    """
    exclude_stages: frozenset[str] = field(default_factory=frozenset)
    min_rs_13w_pct: float | None = None
    exclude_indeterminate: bool = True

    def __post_init__(self) -> None:
        unknown = sorted(set(self.exclude_stages) - set(STAGES))
        if unknown:
            raise ValueError(
                f"unknown Weinstein stage(s) in the technical gate: {unknown}. "
                f"Valid stages are: {STAGES}")

    @property
    def active(self) -> bool:
        return bool(self.exclude_stages) or self.min_rs_13w_pct is not None

    @classmethod
    def from_settings(cls, screen) -> "TechnicalGate":
        return cls(
            exclude_stages=frozenset(screen.technical_gate_exclude_stages),
            min_rs_13w_pct=screen.technical_gate_min_rs_13w,
            exclude_indeterminate=screen.technical_gate_excludes_indeterminate)

    def assess(self, tech: dict) -> Eligibility:
        """Apply the gate to one security's technical bundle."""
        if not self.active:
            return Eligibility(True)

        stage = tech.get("technical_stage")
        if stage is None or stage == INDETERMINATE:
            if self.exclude_indeterminate:
                return Eligibility(False, "EX_NO_TECHNICAL_READ",
                                   EXCLUSION_CODES["EX_NO_TECHNICAL_READ"])
            return Eligibility(True)

        if stage in self.exclude_stages:
            return Eligibility(False, "EX_TECHNICAL_STAGE",
                               f"technical stage is {stage}")

        if self.min_rs_13w_pct is not None:
            rs = tech.get("rs_bm_13w_pct")
            if rs is None:
                return Eligibility(False, "EX_WEAK_RS",
                                   "13-week relative strength unavailable")
            if rs < self.min_rs_13w_pct:
                return Eligibility(
                    False, "EX_WEAK_RS",
                    f"13w RS {rs:+.1f}% < floor {self.min_rs_13w_pct:+.1f}%")

        return Eligibility(True)


def assess(metrics: dict, market_cap: float | None, weeks_history: int | None,
           liquidity_inr_cr: float | None, *,
           tech: dict | None = None,
           gate: TechnicalGate | None = None) -> Eligibility:
    """
    Apply the gates in order. Returns the first failure, or eligible.

    The archetype gate is NOT applied here: it needs the classification result,
    so the caller applies EX_NO_ARCHETYPE after classify() returns nothing.

    The technical gate is applied LAST, and is off unless a `gate` is passed.
    Both matter. Gates short-circuit, so the first failure is the recorded code -
    inserting the technical gate earlier would relabel exclusions that have not
    changed meaning. And defaulting it off keeps this function's four-argument
    form identical to the frozen oracle, so the parity suite still compares the
    port rather than the gate.
    """
    if metrics.get("data_error") or not metrics.get("company"):
        return Eligibility(False, "EX_NO_FUNDAMENTALS",
                           EXCLUSION_CODES["EX_NO_FUNDAMENTALS"])
    if market_cap is None:
        return Eligibility(False, "EX_NO_MCAP", EXCLUSION_CODES["EX_NO_MCAP"])
    if market_cap < MCAP_MIN:
        return Eligibility(False, "EX_MCAP_BELOW_BAND",
                           f"Market cap INR {market_cap:,.0f} cr < 1,000 cr")
    if market_cap > MCAP_MAX:
        return Eligibility(False, "EX_MCAP_ABOVE_BAND",
                           f"Market cap INR {market_cap:,.0f} cr > 1,00,000 cr")
    if (metrics.get("fy_count") or 0) < MIN_FY:
        return Eligibility(False, "EX_SHORT_FIN_HISTORY",
                           f"only {metrics.get('fy_count')} annual periods")
    if weeks_history is None or weeks_history < MIN_WEEKS_PRICE:
        return Eligibility(False, "EX_NO_PRICE_HISTORY",
                           EXCLUSION_CODES["EX_NO_PRICE_HISTORY"])
    if liquidity_inr_cr is None or liquidity_inr_cr < LIQ_MIN_CR:
        return Eligibility(False, "EX_ILLIQUID",
                           (f"median daily traded value INR {liquidity_inr_cr} cr"
                            if liquidity_inr_cr is not None else "no volume data"))
    if gate is not None and gate.active:
        return gate.assess(tech or {})
    return Eligibility(True)


def data_quality(metrics: dict) -> str:
    """High/Medium/Low, on how much of the core metric set resolved."""
    core = [metrics.get("revenue_cagr_5y_pct"), metrics.get("roce_median_5y_pct"),
            metrics.get("cfo_pat_5y"), metrics.get("gross_debt_to_equity"),
            metrics.get("promoter_holding_pct")]
    filled = sum(1 for x in core if x is not None)
    dq = "High" if filled >= 5 else ("Medium" if filled >= 3 else "Low")
    if metrics.get("earnings_quality_flag"):
        dq = "Medium" if dq == "High" else "Low"
    return dq


def key_risk(metrics: dict, technical_stage: str | None) -> str:
    """The single most important disqualifying risk visible at Phase 1 depth."""
    fin = bool(metrics.get("is_financial"))
    if metrics.get("cfo_pat_5y") is not None and not fin and metrics["cfo_pat_5y"] < 0.6:
        return "Weak cash conversion (cumulative CFO/PAT below 0.6)"
    if (metrics.get("gross_debt_to_equity") or 0) > 1.8:
        return f"Leverage: debt/equity {metrics.get('gross_debt_to_equity'):.2f}"
    if (metrics.get("promoter_change_pct") or 0) < -3:
        return (f"Promoter stake down {abs(metrics['promoter_change_pct']):.1f}pp "
                f"over 4 quarters")
    if (metrics.get("equity_capital_change_5y_pct") or 0) > 40:
        return (f"Equity capital up {metrics['equity_capital_change_5y_pct']:.0f}% "
                f"in 5y - dilution")
    if metrics.get("earnings_quality_flag"):
        return metrics["earnings_quality_flag"]
    if technical_stage in ("Stage 4 decline", "Stage 3 distribution"):
        return f"Technically in {technical_stage}"
    return "None identified at Phase 1 depth"
