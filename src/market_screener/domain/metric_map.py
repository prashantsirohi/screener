"""
Mapping from scraped statement row labels to stable metric_ids.

The aggregator's row labels are display strings and will drift ("Sales" became
"Revenue" for financial companies already). Pinning them to a controlled
metric_id means a wording change is a one-line map entry, not a schema change
and not a silently missing metric.
"""

from __future__ import annotations

import re

STATEMENTS = {
    "profit_loss": "annual",
    "balance_sheet": "annual",
    "cash_flow": "annual",
    "ratios": "annual",
    "quarters": "quarter",
    "shareholding": "quarter",
    "top_ratios": "snapshot",
}

# Deliberately NOT aliased: "Revenue"/"Financing Profit"/"Financing Margin %"
# are the labels screener uses for lenders, and folding them into
# "Sales"/"Operating Profit"/"OPM %" is lossy in two ways. It makes the payload
# round trip unfaithful, and - the real damage - metrics.is_financial() detects
# a bank or NBFC by the presence of a "Financing Profit" row, so the aliasing
# would silently reclassify every lender as a non-financial and then apply
# CFO/PAT and debt/equity tests that are meaningless for them.
#
# Cross-company comparability is handled at query time instead, by coalescing
# the two metric_ids where a single top-line figure is wanted.
LABEL_ALIASES = {
    "eps_in_rs": "eps",
}

# Units that cannot be inferred from the label text.
UNIT_OVERRIDES = {
    "profit_loss.eps": "inr",
    "top_ratios.current_price": "inr",
    "top_ratios.book_value": "inr",
    "top_ratios.face_value": "inr",
    "top_ratios.high_52w": "inr",
    "top_ratios.low_52w": "inr",
    "top_ratios.market_cap": "inr_cr",
    "top_ratios.stock_pe": "ratio",
    "top_ratios.dividend_yield": "pct",
    "top_ratios.roce": "pct",
    "top_ratios.roe": "pct",
    "cash_flow.cfo_op": "ratio",
}

HIGHER_IS_BETTER = {
    "profit_loss.sales", "profit_loss.operating_profit", "profit_loss.net_profit",
    "profit_loss.eps", "profit_loss.opm_pct", "ratios.roce_pct",
    "cash_flow.cash_from_operating_activity", "cash_flow.free_cash_flow",
    "top_ratios.roce", "top_ratios.roe",
}
LOWER_IS_BETTER = {
    "balance_sheet.borrowings", "ratios.debtor_days", "ratios.inventory_days",
    "ratios.working_capital_days", "ratios.cash_conversion_cycle",
    "profit_loss.interest",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(label: str) -> str:
    s = label.strip().lower()
    s = s.replace("%", " pct ").replace("/", " ").replace("+", "")
    s = _SLUG_RE.sub("_", s).strip("_")
    return LABEL_ALIASES.get(s, s)


def metric_id(statement: str, label: str) -> str:
    return f"{statement}.{slugify(label)}"


def growth_metric_id(table: str, range_label: str) -> str:
    """
    The compounded-growth block is keyed by a range ("5 Years"), not a date, so
    the range is folded into the metric_id rather than forced into report_date.

        ("Compounded Sales Growth", "5 Years") -> growth.compounded_sales_growth__5_years
    """
    return f"growth.{slugify(table)}__{slugify(range_label)}"


def parse_growth_metric_id(mid: str) -> tuple[str, str] | None:
    """Inverse of growth_metric_id: -> (table_slug, range_slug)."""
    if not mid.startswith("growth."):
        return None
    body = mid[len("growth."):]
    if "__" not in body:
        return None
    table, rng = body.split("__", 1)
    return table, rng


def infer_unit(statement: str, label: str, mid: str) -> str:
    if mid in UNIT_OVERRIDES:
        return UNIT_OVERRIDES[mid]
    low = label.lower()
    if "%" in label or low.endswith(" pct"):
        return "pct"
    if "days" in low or "cycle" in low:
        return "days"
    if "shareholders" in low:
        return "count"
    if statement == "shareholding":
        return "pct"
    if statement == "ratios":
        return "pct" if "roce" in low else "days"
    if statement == "top_ratios":
        return "ratio"
    return "inr_cr"


def higher_is_better(mid: str) -> bool | None:
    if mid in HIGHER_IS_BETTER:
        return True
    if mid in LOWER_IS_BETTER:
        return False
    return None


def describe(statement: str, label: str) -> dict:
    mid = metric_id(statement, label)
    return {
        "metric_id": mid,
        "metric_label": label,
        "statement": statement,
        "unit": infer_unit(statement, label, mid),
        "higher_is_better": higher_is_better(mid),
        "description": None,
    }
