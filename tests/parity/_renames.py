"""
Metric keys renamed between the frozen oracle and the port.

The port stopped claiming more than the data supports: the aggregator exposes no
cash, so the leverage figure is gross; and the EPS CAGR is reported, exceptional
items included. Same arithmetic, honest label.

The parity suite must therefore translate the oracle's bundle before comparing,
NOT exempt the fields - the whole point is that the values are still identical.
This lives in one module because two parity tests need it and a second copy
would eventually disagree with the first.
"""

from __future__ import annotations

# oracle key -> port key
RENAMED_IN_PORT = {
    "net_debt_to_equity": "gross_debt_to_equity",
    "eps_cagr_5y_pct": "reported_eps_cagr_5y_pct",
    "eps_cagr_3y_pct": "reported_eps_cagr_3y_pct",
}

# The oracle carried `debt_to_equity` and `net_debt_to_equity` holding the same
# value; the port keeps one honest key instead of two names for one number.
DROPPED_IN_PORT = {"debt_to_equity"}


def translate(bundle: dict) -> dict:
    """An oracle metric bundle, keyed the way the port keys it."""
    return {RENAMED_IN_PORT.get(k, k): v for k, v in bundle.items()
            if k not in DROPPED_IN_PORT}
