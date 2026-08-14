"""
Phase 2 valuation.

Phase 1 refuses to judge price: "Undervalued" is a banned tag there and QC03
enforces it, because a discovery screen that also opines on value tends to find
cheap bad businesses. Phase 2 is where price enters.

Several methods, each kept separately, and deliberately no single fair value.
Every input here is a reported trailing figure from an aggregator - no forward
estimates exist anywhere in this system - so one blended number would imply a
precision the data cannot support. Where two methods disagree, that disagreement
IS the finding and the assessment records it.

The `unassessable` verdict is a real answer, not a gap. A loss-making company has
no meaningful P/E, and reporting it as "expensive" would be a category error.
"""

from __future__ import annotations

from dataclasses import dataclass

# Roughly the Indian 10-year government yield. Used only as the discount anchor
# for the reverse DCF and the earnings-yield comparison; it is a constant rather
# than a live series because nothing else in this system needs a rate curve, and
# a stale curve would be worse than an explicit assumption.
RISK_FREE_PCT = 7.0
EQUITY_PREMIUM_PCT = 5.5
COST_OF_EQUITY_PCT = RISK_FREE_PCT + EQUITY_PREMIUM_PCT   # 12.5

VERDICTS = ("cheap", "fair", "full", "stretched", "unassessable")


@dataclass(frozen=True)
class Method:
    name: str
    value: float | None
    verdict: str
    basis: str


def _peg(metrics: dict) -> Method:
    pe = metrics.get("stock_pe")
    growth = max([g for g in (metrics.get("reported_eps_cagr_3y_pct"),
                              metrics.get("reported_eps_cagr_5y_pct"))
                  if g is not None], default=None)
    if not pe or pe <= 0:
        return Method("peg", None, "unassessable",
                      "no positive trailing P/E - loss-making or no earnings")
    if not growth or growth <= 0:
        return Method("peg", None, "unassessable",
                      "no positive realised EPS growth to compare the multiple against")
    peg = pe / growth
    verdict = ("cheap" if peg < 0.8 else "fair" if peg < 1.5
               else "full" if peg < 2.5 else "stretched")
    return Method("peg", round(peg, 3), verdict,
                  f"trailing P/E {pe:.1f} over realised EPS CAGR {growth:.1f}% "
                  f"(reported EPS, exceptional items included)")


def _pb_vs_roe(metrics: dict) -> Method:
    """
    A company earning its cost of equity is worth about book. The justified
    multiple is ROE/COE, so P/B relative to that says whether the market is
    paying more than the returns support.
    """
    px, bv = metrics.get("current_price_inr"), metrics.get("book_value_inr")
    roe = metrics.get("roe_latest_pct") or metrics.get("screener_roe_5y")
    if not px or not bv or bv <= 0:
        return Method("pb_vs_roe", None, "unassessable", "no positive book value")
    pb = px / bv
    if not roe or roe <= 0:
        return Method("pb_vs_roe", round(pb, 2), "unassessable",
                      f"P/B {pb:.2f} but no positive ROE to justify a multiple")
    justified = roe / COST_OF_EQUITY_PCT
    ratio = pb / justified if justified else None
    verdict = ("cheap" if ratio < 0.7 else "fair" if ratio < 1.3
               else "full" if ratio < 2.0 else "stretched")
    return Method("pb_vs_roe", round(ratio, 3), verdict,
                  f"P/B {pb:.2f} against {justified:.2f} justified by ROE "
                  f"{roe:.1f}% at a {COST_OF_EQUITY_PCT:.1f}% cost of equity")


def _earnings_yield(metrics: dict) -> Method:
    pe = metrics.get("stock_pe")
    if not pe or pe <= 0:
        return Method("earnings_yield", None, "unassessable", "no positive P/E")
    ey = 100.0 / pe
    spread = ey - RISK_FREE_PCT
    verdict = ("cheap" if spread > 2 else "fair" if spread > -2
               else "full" if spread > -4 else "stretched")
    return Method("earnings_yield", round(ey, 2), verdict,
                  f"earnings yield {ey:.1f}% against a {RISK_FREE_PCT:.1f}% "
                  f"risk-free rate ({spread:+.1f}pp)")


def _pe_percentile(metrics: dict, pe_history: list[float] | None) -> Method:
    """
    Where the current multiple sits in the company's own history.

    Cross-company multiples are barely comparable across these ten archetypes;
    a company against its own five-year range is a fairer question.
    """
    pe = metrics.get("stock_pe")
    if not pe or pe <= 0:
        return Method("pe_percentile_5y", None, "unassessable", "no positive P/E")
    hist = [p for p in (pe_history or []) if p and p > 0]
    if len(hist) < 4:
        return Method("pe_percentile_5y", None, "unassessable",
                      f"only {len(hist)} year(s) of usable P/E history")
    pct = 100.0 * sum(1 for h in hist if h <= pe) / len(hist)
    verdict = ("cheap" if pct <= 25 else "fair" if pct <= 60
               else "full" if pct <= 85 else "stretched")
    return Method("pe_percentile_5y", round(pct, 1), verdict,
                  f"current P/E {pe:.1f} is at the {pct:.0f}th percentile of "
                  f"{len(hist)} years of its own history")


def _reverse_dcf(metrics: dict) -> Method:
    """
    The growth rate the current price implies, on a crude perpetuity.

    Deliberately crude - a full DCF on aggregator trailing figures would be
    false precision. Its job is to say what the market is assuming, so that
    assumption can be judged against the realised record.

        P = E / (r - g)  =>  g = r - E/P
    """
    pe = metrics.get("stock_pe")
    if not pe or pe <= 0:
        return Method("reverse_dcf_growth", None, "unassessable", "no positive P/E")
    implied = COST_OF_EQUITY_PCT - (100.0 / pe)
    realised = max([g for g in (metrics.get("reported_eps_cagr_3y_pct"),
                                metrics.get("reported_eps_cagr_5y_pct"))
                    if g is not None], default=None)
    if realised is None:
        return Method("reverse_dcf_growth", round(implied, 2), "unassessable",
                      f"price implies {implied:.1f}% perpetual growth; no "
                      f"realised record to judge it against")
    gap = implied - realised
    # Implied growth well BELOW what the company has actually delivered is the
    # cheap case; well above it means the price needs an acceleration.
    verdict = ("cheap" if gap < -8 else "fair" if gap < 2
               else "full" if gap < 8 else "stretched")
    return Method("reverse_dcf_growth", round(implied, 2), verdict,
                  f"price implies {implied:.1f}% perpetual growth at a "
                  f"{COST_OF_EQUITY_PCT:.1f}% discount rate, against {realised:.1f}% "
                  f"realised ({gap:+.1f}pp)")


_SCORE = {"cheap": 100.0, "fair": 70.0, "full": 35.0, "stretched": 0.0}


def assess(metrics: dict, pe_history: list[float] | None = None
           ) -> tuple[list[Method], float | None, str]:
    """Every method, a 0-100 score, and the overall verdict."""
    methods = [
        _peg(metrics),
        _pb_vs_roe(metrics),
        _earnings_yield(metrics),
        _pe_percentile(metrics, pe_history),
        _reverse_dcf(metrics),
    ]
    usable = [m for m in methods if m.verdict != "unassessable"]
    if not usable:
        return methods, None, "unassessable"

    score = sum(_SCORE[m.verdict] for m in usable) / len(usable)
    # The overall verdict is the MEDIAN method, not the mean score bucketed.
    # Averaging "cheap" and "stretched" into "fair" would report agreement that
    # does not exist; the median at least names a verdict some method held.
    order = sorted(usable, key=lambda m: _SCORE[m.verdict])
    verdict = order[len(order) // 2].verdict
    return methods, round(score, 2), verdict


def disagreement(methods: list[Method]) -> bool:
    """True when the usable methods span both ends of the range."""
    v = {m.verdict for m in methods if m.verdict != "unassessable"}
    return bool(v & {"cheap"}) and bool(v & {"full", "stretched"})
