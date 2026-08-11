"""
Corporate-announcement classification.

Two versions coexist, deliberately:

* **v1** is the exact pattern set the frozen Phase 1 baseline used. It is kept
  byte-for-byte so the port can be shown to reproduce the baseline. Changing the
  taxonomy changes archetypes and secondary tags, which would make a parity
  failure indistinguishable from a regression.
* **v2** is the richer taxonomy modelled on the market_intel project: tiered
  categories with importance weights, covering results, orders, capex, guidance
  and management change as well as the structural events v1 looks for.

`announcement_classification` is versioned, so both can be stored side by side
and the difference measured before anything switches over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# v1 - frozen. Do not edit; the Phase 1 baseline depends on these exact patterns.
# ---------------------------------------------------------------------------

V1_PATTERNS: list[tuple[str, str, str]] = [
    (r"scheme of arrangement|demerger|de-merger|demerged|composite scheme",
     "Demerger / scheme of arrangement", "Demerger/SOTP unlocking"),
    # "substantial acquisition of shares" alone is the SAST regulation title and
    # appears on routine 2%-threshold disclosures - not evidence of an open offer.
    (r"open offer|detailed public statement|letter of offer|"
     r"public announcement under regulation 3|change in control",
     "Open offer / control change", "Regulatory catalyst"),
    (r"\bdelisting\b|voluntary delisting", "Delisting", "Regulatory catalyst"),
    (r"buy-?back of equity|buyback of equity|buy back of equity",
     "Buyback", "Regulatory catalyst"),
    (r"capital reduction|reduction of (share )?capital", "Capital reduction",
     "Demerger/SOTP unlocking"),
    (r"slump sale|sale of (the )?(undertaking|business|division|subsidiary)|divestment|"
     r"stake sale|monetisation|monetization",
     "Asset / business sale", "Demerger/SOTP unlocking"),
    (r"initial public offer(ing)? of .*subsidiary|listing of .*subsidiary|"
     r"unlocking value", "Subsidiary listing", "Demerger/SOTP unlocking"),
    (r"amalgamation|merger of|merged with", "Amalgamation", "Demerger/SOTP unlocking"),
    (r"insolvency|nclt|resolution plan|corporate insolvency",
     "Insolvency / resolution", "Governance risk"),
    (r"qualified institutional placement|\bqip\b|preferential (issue|allotment)|"
     r"\bwarrants?\b", "Equity raise", "Governance risk"),
    (r"auditor.{0,40}(resign|qualif)|resignation of (statutory )?auditor",
     "Auditor change/qualification", "Governance risk"),
    (r"\bsebi\b.{0,60}(order|penalt|show cause|adjudicat)",
     "Regulatory action", "Governance risk"),
]

_V1_COMPILED = [(re.compile(p, re.I), label, tag) for p, label, tag in V1_PATTERNS]


def classify_v1(text: str) -> list[tuple[str, str]]:
    """All matching (event_class, secondary_tag) pairs - v1 is multi-label."""
    t = (text or "").lower()
    return [(label, tag) for rx, label, tag in _V1_COMPILED if rx.search(t)]


# ---------------------------------------------------------------------------
# v2 - tiered taxonomy
# ---------------------------------------------------------------------------

TIER_A = {
    "demerger", "management_change", "regulatory_legal", "major_order_win",
    "capex_expansion", "buyback", "promoter_activity", "open_offer",
    "delisting", "asset_sale", "rating_downgrade", "insolvency",
}
TIER_B = {
    "results", "board_meeting", "dividend", "fundraise", "mna_partnership",
    "capital_reduction", "subsidiary_listing", "rating_upgrade", "guidance",
}
TIER_C = {"credit_rating", "clarification", "auditor_change"}
IGNORE = {
    "newspaper_publication", "investor_meet", "agm_notice",
    "compliance_certificate", "loss_of_certificate", "analyst_call",
    "trading_window", "nav_update",
}

CATEGORY_IMPORTANCE = {
    "demerger": 9.3, "regulatory_legal": 9.5, "buyback": 9.2, "open_offer": 9.4,
    "delisting": 9.0, "asset_sale": 8.6, "insolvency": 9.1,
    "management_change": 8.4, "major_order_win": 8.2, "capex_expansion": 8.0,
    "promoter_activity": 8.1, "rating_downgrade": 8.3,
    "results": 7.5, "fundraise": 7.2, "mna_partnership": 7.4,
    "capital_reduction": 7.6, "subsidiary_listing": 7.8, "dividend": 6.5,
    "board_meeting": 6.0, "rating_upgrade": 6.8, "guidance": 6.9,
    "credit_rating": 5.5, "auditor_change": 5.8, "clarification": 4.0,
    "general": 5.0,
}
for _c in IGNORE:
    CATEGORY_IMPORTANCE.setdefault(_c, 2.0)

# Order matters: first match wins, most specific first.
V2_PATTERNS: list[tuple[str, str]] = [
    ("demerger", r"scheme of arrangement|demerger|de-merger|demerged|composite scheme|spin-?off|hive[- ]off"),
    ("open_offer", r"open offer|detailed public statement|letter of offer|"
                   r"public announcement under regulation 3|change in control"),
    ("delisting", r"\bdelisting\b|voluntary delisting"),
    ("buyback", r"buy-?back of equity|buyback of equity|buy back of equity|share repurchase"),
    ("capital_reduction", r"capital reduction|reduction of (share )?capital"),
    ("asset_sale", r"slump sale|sale of (the )?(undertaking|business|division|subsidiary)|"
                   r"divestment|stake sale|monetisation|monetization"),
    ("subsidiary_listing", r"initial public offer(ing)? of .*subsidiary|"
                           r"listing of .*subsidiary|unlocking value"),
    ("insolvency", r"insolvency|\bnclt\b|resolution plan|corporate insolvency"),
    ("regulatory_legal", r"\bsebi\b.{0,60}(order|penalt|show cause|adjudicat)|"
                         r"\bshow cause notice\b|adjudication order"),
    ("auditor_change", r"auditor.{0,40}(resign|qualif)|resignation of (statutory )?auditor"),
    ("mna_partnership", r"amalgamation|merger of|merged with|acquisition of|"
                        r"joint venture|strategic partnership"),
    ("major_order_win", r"order win|order received|contract awarded|letter of award|"
                        r"\bloa\b|l1 bidder|work order|purchase order|bagging|awarded"),
    ("capex_expansion", r"capacity expansion|new plant|greenfield|brownfield|"
                        r"commercial production|commissioning|capex"),
    ("management_change", r"resignation of (the )?(managing director|chief|whole[- ]time)|"
                          r"appointment of (the )?(managing director|chief|ceo|cfo)|"
                          r"cessation of"),
    ("promoter_activity", r"promoter.{0,40}(pledg|encumbr|acquisition|disposal)|"
                          r"revocation of pledge|creation of pledge"),
    ("fundraise", r"qualified institutional placement|\bqip\b|"
                  r"preferential (issue|allotment)|\bwarrants?\b|rights issue|"
                  r"fund rais"),
    ("rating_downgrade", r"rating.{0,40}(downgrad|revised downward)|downgrade"),
    ("rating_upgrade", r"rating.{0,40}(upgrad|revised upward)|upgrade in rating"),
    ("credit_rating", r"credit rating|rating rationale|reaffirm"),
    ("results", r"financial results|unaudited results|audited results|"
                r"statement of (standalone|consolidated) results"),
    ("guidance", r"guidance|outlook for"),
    ("board_meeting", r"board meeting|outcome of the board"),
    ("dividend", r"dividend"),
    ("trading_window", r"trading window|closure of trading"),
    ("agm_notice", r"annual general meeting|\bagm\b|postal ballot|\begm\b"),
    ("investor_meet", r"investor (meet|presentation|conference)|analyst meet"),
    ("analyst_call", r"earnings call|conference call|transcript"),
    ("newspaper_publication", r"newspaper (publication|advertisement)|"
                              r"publication in newspaper"),
    ("compliance_certificate", r"compliance certificate|certificate under regulation"),
    ("loss_of_certificate", r"loss of (share )?certificate|duplicate share certificate"),
]

_V2_COMPILED = [(cat, re.compile(rx, re.I)) for cat, rx in V2_PATTERNS]


@dataclass(frozen=True)
class Classification:
    category: str
    tier: str
    importance: float
    matched: str | None


def tier_of(category: str) -> str:
    if category in TIER_A:
        return "A"
    if category in TIER_B:
        return "B"
    if category in IGNORE:
        return "IGNORE"
    if category in TIER_C:
        return "C"
    return "C"


def classify_v2(text: str) -> Classification:
    """Single-label, first match wins over an explicit priority order."""
    t = " ".join((text or "").split()).lower()
    if not t:
        return Classification("general", tier_of("general"),
                              CATEGORY_IMPORTANCE["general"], None)
    for cat, rx in _V2_COMPILED:
        m = rx.search(t)
        if m:
            return Classification(cat, tier_of(cat),
                                  CATEGORY_IMPORTANCE.get(cat, 5.0), m.group(0)[:60])
    return Classification("general", tier_of("general"),
                          CATEGORY_IMPORTANCE["general"], None)
