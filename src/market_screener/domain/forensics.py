"""
Phase 2 forensic checks over the statement history.

Phase 1 asks whether a company presents an identifiable return mechanism. Phase 2
asks whether the numbers describing it are trustworthy. These are different
questions and they fail differently: a Phase 1 gate excludes a company outright,
whereas a forensic finding is graded, because "receivables grew faster than
sales" is a reason to look harder, not a verdict.

Everything here reads the reconstructed payload directly rather than the Phase 1
metric bundle. Two reasons. The bundle omits the working-capital series (debtor
days, inventory days, cash conversion cycle) that half these checks need; and
`metrics.py` is a verbatim port covered by a parity oracle, so extending it to
serve Phase 2 would put the frozen comparison at risk for no gain.

WHAT THESE CANNOT SEE. Auditor qualifications, related-party transactions,
promoter pledging, contingent liabilities and CWIP ageing are the checks a
forensic analyst would reach for first, and all of them need the annual report.
No filing has ever been fetched. These tests work the statement history hard, but
a clean result here means "nothing visible in the numbers", not "clean".
"""

from __future__ import annotations

from dataclasses import dataclass

from .metrics import fy_cols, safe_div

# severity ordering, worst last
SEVERITIES = ("clean", "watch", "concern", "disqualifying")


@dataclass(frozen=True)
class Flag:
    code: str
    severity: str
    evidence: str
    value: float | None = None


FLAG_MEANINGS = {
    "ACCRUALS_HIGH": (
        "Reported profit is not arriving as cash: the gap between PAT and CFO is "
        "large relative to the asset base."),
    "CASH_CONVERSION_WEAK": (
        "Cumulative CFO covers little of cumulative PAT over the available "
        "history."),
    "RECEIVABLES_OUTPACING_SALES": (
        "Debtor days rising while revenue growth slows - revenue may be being "
        "recognised ahead of collection."),
    "WORKING_CAPITAL_DECAY": (
        "The cash conversion cycle has lengthened materially, tying up cash the "
        "income statement does not show."),
    "INVENTORY_BUILD": (
        "Inventory days rising well ahead of sales - possible obsolescence or "
        "channel stuffing."),
    "OTHER_INCOME_DEPENDENT": (
        "A large share of pre-tax profit is other income rather than operations."),
    "DILUTION": (
        "Share capital has expanded materially, so per-share growth overstates "
        "what an existing holder received."),
    "PROMOTER_SELLING": (
        "Promoter holding has fallen over the last four quarters."),
    "LEVERAGE_HIGH": (
        "Gross borrowings are large relative to equity. Cash is not separately "
        "reported, so true net leverage may be lower."),
    "INTEREST_COVER_THIN": (
        "Operating profit covers interest only narrowly."),
    "MARGIN_AT_PEAK": (
        "Operating margin is at or near its five-year peak, so current earnings "
        "are unlikely to be a mid-cycle base."),
    "CWIP_STALLED": (
        "Capital work in progress is large and has not converted into fixed "
        "assets - the project may be delayed or impaired."),
    "FCF_PERSISTENTLY_NEGATIVE": (
        "Free cash flow has been negative in most of the available years."),
}


def _series(payload: dict, statement: str, label: str) -> list[float]:
    """A statement row as an ordered annual series, missing years dropped."""
    block = (payload.get(statement) or {}).get(label) or {}
    out = []
    for col in fy_cols(block):
        v = block.get(col)
        if v is not None:
            out.append(float(v))
    return out


def _trend(vals: list[float], years: int = 3) -> float | None:
    """Change from `years` back to latest, as a percentage of the earlier value."""
    if len(vals) < years + 1:
        return None
    old, new = vals[-(years + 1)], vals[-1]
    if not old:
        return None
    return (new - old) / abs(old) * 100.0


def assess(payload: dict, metrics: dict) -> list[Flag]:
    """
    Every forensic finding for one company, worst-first.

    `metrics` is the Phase 1 bundle, reused where it already computes something
    correctly; `payload` supplies the series it does not carry.
    """
    if metrics.get("data_error"):
        return []

    flags: list[Flag] = []
    fin = bool(metrics.get("is_financial"))

    def add(code, severity, evidence, value=None):
        flags.append(Flag(code, severity, evidence, value))

    # ---- earnings quality ---------------------------------------------------
    # Accrual ratio. Financial companies are exempt: for a lender, CFO swings
    # with loan-book growth, so a large PAT-CFO gap is how growth looks rather
    # than a warning.
    pat = metrics.get("net_profit_latest_inr_cr")
    cfo = metrics.get("cfo_latest_inr_cr")
    assets = metrics.get("total_assets_inr_cr")
    if not fin and None not in (pat, cfo) and assets:
        accrual = (pat - cfo) / assets * 100.0
        if accrual >= 15:
            add("ACCRUALS_HIGH", "concern",
                f"PAT exceeds CFO by {accrual:.1f}% of total assets", accrual)
        elif accrual >= 8:
            add("ACCRUALS_HIGH", "watch",
                f"PAT exceeds CFO by {accrual:.1f}% of total assets", accrual)

    cp = metrics.get("cfo_pat_5y") or metrics.get("cfo_pat_3y")
    if not fin and cp is not None:
        if cp < 0.4:
            add("CASH_CONVERSION_WEAK", "disqualifying",
                f"cumulative CFO/PAT {cp:.2f} over {metrics.get('cfo_pat_period')}", cp)
        elif cp < 0.6:
            add("CASH_CONVERSION_WEAK", "concern",
                f"cumulative CFO/PAT {cp:.2f} over {metrics.get('cfo_pat_period')}", cp)
        elif cp < 0.8:
            add("CASH_CONVERSION_WEAK", "watch",
                f"cumulative CFO/PAT {cp:.2f} over {metrics.get('cfo_pat_period')}", cp)

    oi = metrics.get("other_income_to_pbt_pct")
    if oi is not None and oi > 35:
        add("OTHER_INCOME_DEPENDENT", "concern" if oi > 50 else "watch",
            f"other income is {oi:.0f}% of PBT", oi)

    # ---- working capital ----------------------------------------------------
    # Meaningless for lenders, who have no trade receivable cycle in this sense.
    if not fin:
        debtor = _series(payload, "ratios", "Debtor Days")
        sales = _series(payload, "profit_loss", "Sales") or \
            _series(payload, "profit_loss", "Revenue")
        d_trend, s_trend = _trend(debtor), _trend(sales)
        if d_trend is not None and s_trend is not None and d_trend > 25 \
                and d_trend > s_trend + 20:
            add("RECEIVABLES_OUTPACING_SALES",
                "concern" if d_trend > 50 else "watch",
                f"debtor days +{d_trend:.0f}% over 3y against sales "
                f"{s_trend:+.0f}%", d_trend)

        ccc = _series(payload, "ratios", "Cash Conversion Cycle")
        c_trend = _trend(ccc)
        # Only meaningful off a positive base: a cycle going from -5 to +5 days
        # is a 200% "deterioration" and almost nothing in cash terms.
        if c_trend is not None and len(ccc) >= 4 and ccc[-4] > 15 and c_trend > 40:
            add("WORKING_CAPITAL_DECAY", "concern" if c_trend > 80 else "watch",
                f"cash conversion cycle {ccc[-4]:.0f} -> {ccc[-1]:.0f} days", c_trend)

        inv = _series(payload, "ratios", "Inventory Days")
        i_trend = _trend(inv)
        if i_trend is not None and s_trend is not None and i_trend > 30 \
                and i_trend > s_trend + 25:
            add("INVENTORY_BUILD", "watch",
                f"inventory days +{i_trend:.0f}% over 3y against sales "
                f"{s_trend:+.0f}%", i_trend)

    # ---- capital structure and ownership ------------------------------------
    dil = metrics.get("equity_capital_change_5y_pct")
    if dil is not None and dil > 25:
        add("DILUTION", "concern" if dil > 60 else "watch",
            f"share capital +{dil:.0f}% over 5y", dil)

    pch = metrics.get("promoter_change_pct")
    if pch is not None and pch < -1.0:
        add("PROMOTER_SELLING", "concern" if pch < -5 else "watch",
            f"promoter holding {pch:+.1f}pp over 4 quarters", pch)

    de = metrics.get("gross_debt_to_equity")
    if not fin and de is not None and de > 1.5:
        add("LEVERAGE_HIGH", "concern" if de > 2.5 else "watch",
            f"gross debt/equity {de:.2f} (cash not separately reported)", de)

    ic = metrics.get("interest_cover_x")
    if not fin and ic is not None and ic < 3:
        add("INTEREST_COVER_THIN", "disqualifying" if ic < 1.5 else "concern",
            f"interest cover {ic:.1f}x", ic)

    # ---- cycle position and capex -------------------------------------------
    peak = metrics.get("opm_vs_peak_pp")
    if peak is not None and peak >= -1.0:
        add("MARGIN_AT_PEAK", "watch",
            f"operating margin {metrics.get('opm_latest_pct'):.1f}% is "
            f"{abs(peak):.1f}pp from its 5y peak", peak)

    cwip_ratio = metrics.get("cwip_to_gross_block_pct")
    cwip_chg = metrics.get("cwip_change_pct")
    if cwip_ratio is not None and cwip_ratio > 20 and cwip_chg is not None \
            and cwip_chg > -10:
        add("CWIP_STALLED", "watch",
            f"CWIP is {cwip_ratio:.0f}% of gross block and changed "
            f"{cwip_chg:+.0f}% - no evident commissioning", cwip_ratio)

    fcf_pos = metrics.get("fcf_positive_years_5y")
    if not fin and fcf_pos is not None and fcf_pos <= 1:
        add("FCF_PERSISTENTLY_NEGATIVE", "concern" if fcf_pos == 0 else "watch",
            f"free cash flow positive in {fcf_pos} of the last 5 years", fcf_pos)

    return sorted(flags, key=lambda f: -SEVERITIES.index(f.severity))


def severity_of(flags: list[Flag]) -> str:
    """The worst severity raised."""
    if not flags:
        return "clean"
    return max((f.severity for f in flags), key=SEVERITIES.index)


# What each severity costs. A disqualifying flag alone cannot be outweighed by
# a clean sheet elsewhere - it is handled by the verdict, not by the score.
_COST = {"watch": 4.0, "concern": 12.0, "disqualifying": 30.0}


def score(flags: list[Flag]) -> float:
    """0-100, where 100 is nothing visible in the numbers."""
    return round(max(0.0, 100.0 - sum(_COST[f.severity] for f in flags)), 2)
