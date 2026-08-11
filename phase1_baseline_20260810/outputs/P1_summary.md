# Phase 1 Summary - Indian Equity Screen (Discovery and Bucket Classification)

**Screening date:** 2026-08-11 (Asia/Kolkata)  
**Share-price and market-cap date:** 2026-08-10  
**Technical-data cutoff:** 2026-08-10 (adjusted weekly bars)  
**Financial-data cutoff:** latest annual period available per company; the modal latest fiscal year across the universe is Mar 2026 (1609 companies), plus trailing-twelve-month and latest-quarter figures where published.

## 1. Universe definition and actual coverage

**Universe claim: FULL for NSE main-board series-EQ equities. Not a full NSE+BSE universe** - BSE-only listings were not screened.

Frame construction:

- Started from NSE's own master list `EQUITY_L.csv` (2,410 rows), filtered to `SERIES == EQ`, giving **2,086 main-board equity symbols**. Every one of these 2,086 was evaluated and appears in `P1_screened_universe.csv`.
- Prices and turnover come from the NSE full bhavcopy for the price date.
- NSE index constituent files (Nifty Total Market 752, Microcap 250, Nifty 500) supply NSE's own industry classification; 742 symbols carry it, the remainder are recorded as `Not disclosed`.
- Companies listed only on BSE, SME-platform scrips, ETFs, REITs/InvITs, government securities and non-EQ series were never in the frame.

**Coverage achieved:** 2086 securities evaluated; 987 eligible; 150 selected as Phase 1 candidates.

## 2. Funnel

| Stage | Count |
|---|---:|
| NSE series-EQ symbols evaluated | 2086 |
| Excluded | 1099 |
| Eligible after all gates | 987 |
| Selected as Phase 1 candidates | 150 |

Selection rule applied: top 150 by preliminary priority score (score floor 68.1).

### Exclusions by code

| Code | Count | Meaning |
|---|---:|---|
| `EX_MCAP_BELOW_BAND` | 510 | Market cap below the INR 1,000 cr floor. |
| `EX_NO_MCAP` | 307 | Market capitalisation not available from any source used. |
| `EX_MCAP_ABOVE_BAND` | 118 | Market cap above the INR 1,00,000 cr ceiling. |
| `EX_NO_PRICE_HISTORY` | 71 | Fewer than 40 adjusted weekly bars - Weinstein staging impossible. |
| `EX_ILLIQUID` | 51 | Median daily traded value below INR 1.0 cr over the trailing 13 weeks. |
| `EX_SHORT_FIN_HISTORY` | 39 | Fewer than 3 annual reporting periods available. |
| `EX_NO_ARCHETYPE` | 3 | No archetype discovery test passed on the available data; the company does not present an identifiable return mechanism at Phase 1 depth. |

## 3. Eligibility gates and formulas

Applied in order; the first gate a company fails is the recorded exclusion code.

| Gate | Rule |
|---|---|
| Financial record | A parseable statement set must exist |
| Market cap | INR 1,000 cr <= market cap <= INR 1,00,000 cr |
| Financial history | >= 3 annual reporting periods |
| Price history | >= 40 adjusted weekly bars (the 40-week MA must exist) |
| Liquidity | 13-week median daily traded value >= INR 1.0 cr |
| Classification | at least one archetype discovery test must pass |

**Liquidity definition.** `liquidity_value_inr_cr` is the **13-week median of weekly traded value, divided by 5** to express a median daily figure in INR crore. Weekly traded value is `mean(high, low, adjusted close) x weekly volume`. This is a labelled substitute for a true 3-month median of daily turnover: only weekly bars were available across the whole universe. It is systematically slightly smoother than a true daily median and should not be read to two decimal places.

**Key formulas.**

```
revenue_cagr_5y   = (Rev[FY0] / Rev[FY-5]) ^ (1/5) - 1        # only if both > 0
eps_cagr_5y       = (EPS[FY0] / EPS[FY-5]) ^ (1/5) - 1        # only if both > 0
cfo_pat_ratio     = sum(CFO, last 5 FY) / sum(PAT, last 5 FY)
net_debt_to_equity= Borrowings / (Equity capital + Reserves)  # see caveat below
ma30w, ma40w      = 30- and 40-week SMA of adjusted close
ma_slope_pct      = MA[t] / MA[t-13] - 1                      # 13-week direction
rs_bm_13w_pct     = (P/Bench)[t] / (P/Bench)[t-13] - 1        # vs Nifty 500
liquidity_inr_cr  = median(weekly traded value, 13w) / 5 / 1e7
```

## 4. Normalisation approach and its limits

Phase 1 uses **reported** figures. EPS and PAT have **not** been normalised for exceptional items, because that requires the filings themselves - which is Phase 2 work. Three mitigations were applied instead:

1. Where other income exceeds 35% of profit before tax, the company carries an explicit `earnings_quality_flag` and its EPS-derived metrics are treated as provisional (data-quality confidence is downgraded).
2. Cyclical candidates were tested on **margin distance from the 5-year peak** and on quarterly margin direction, never on peak EPS.
3. `preliminary_valuation_value` is a trailing P/E (or P/B for lenders) and is explicitly labelled preliminary. **No intrinsic value, target price or 'Undervalued' tag has been assigned in Phase 1.**

**Balance-sheet caveat.** The aggregator does not expose cash and equivalents as a separate line, so `net_debt_to_equity` is computed as **gross borrowings / net worth**. For cash-rich companies this overstates leverage. Phase 2 must recompute true net debt from the balance sheet. The field is left blank for banks, NBFCs and insurers, where the ratio is not meaningful.

**Lenders.** For companies classified as financial (by NSE industry label or by the presence of a `Financing Profit` line), CFO/PAT and debt/equity are suppressed: a growing loan book produces structurally negative operating cash flow, and borrowings are raw material rather than leverage. ROE replaces ROCE for these names.

## 5. Three-axis classification

**Axis A** - exactly one primary archetype per company, chosen as the highest-scoring of ten archetype-specific discovery tests (no single universal screen). Each test returns 0 when its own preconditions are absent, so an archetype is never assigned by default.

**Axis B** - secondary tags drawn only from the controlled vocabulary. **`Undervalued` was not assigned to any company**, as required: it needs the three-condition test in Phase 2.

**Axis C** - Weinstein stage computed arithmetically from adjusted weekly closes: the 30- and 40-week moving averages and their 13-week slopes, relative strength versus the Nifty 500 and the relevant sector index, base duration and depth, pivot, volume confirmation, distance from the 52-week high, and overhead supply. No stage was assigned by judgement.

### Eligible universe by archetype

| Archetype | Eligible | Selected |
|---|---:|---:|
| Quality compounder | 255 | 53 |
| High-growth company | 207 | 34 |
| Capex operating-leverage candidate | 131 | 10 |
| Event-driven or special situation | 101 | 6 |
| Cyclical recovery | 82 | 19 |
| Turnaround | 59 | 1 |
| Mature value/yield company | 57 | 16 |
| Financial compounder | 48 | 8 |
| Asset-value/SOTP opportunity | 43 | 3 |
| Speculative/emerging business | 4 | 0 |
| **Total** | **987** | **150** |

### Candidates by technical stage

| Stage | Selected | Eligible universe |
|---|---:|---:|
| Early Stage 1 | 41 | 284 |
| Early Stage 2 | 50 | 227 |
| Stage 4 decline | 20 | 216 |
| Mature/extended Stage 2 | 26 | 170 |
| Mature Stage 1 base | 8 | 49 |
| Stage 3 distribution | 5 | 41 |

### Candidates by sector

| Sector | Candidates | Median priority score |
|---|---:|---:|
| Not disclosed | 57 | 72.1 |
| Capital Goods | 24 | 72.8 |
| Automobile and Auto Components | 8 | 73.4 |
| Financial Services | 8 | 75.3 |
| Information Technology | 7 | 73.1 |
| Services | 7 | 73.7 |
| Healthcare | 6 | 70.3 |
| Chemicals | 6 | 74.5 |
| Metals & Mining | 6 | 72.6 |
| Construction Materials | 4 | 70.9 |
| Fast Moving Consumer Goods | 4 | 72.0 |
| Consumer Services | 3 | 68.8 |

## 6. Preliminary priority score

Scored 0-100 on the prescribed weights. Components are recorded per company in the pipeline output.

| Component | Weight | What drives it |
|---|---:|---|
| Financial and balance-sheet quality | 20 | CFO/PAT, leverage, interest cover, FCF consistency (ROE/dilution for lenders) |
| Evidence for archetype thesis | 25 | the archetype's own discovery-test fit |
| Forward catalyst visibility | 20 | latest-quarter revenue/profit/margin direction, CWIP commissioning |
| Preliminary valuation plausibility | 15 | P/E against realised EPS growth, P/B against ROE, dividend yield |
| Governance and disclosure quality | 10 | promoter holding level and trend, 5-year dilution, earnings-quality flag |
| Technical and liquidity confirmation | 10 | Weinstein stage, 13-week relative strength, traded value |

## 7. Top 20 Phase 2 priorities

| # | Company | Symbol | Mkt cap (cr) | Archetype | Stage | Score | Why it ranks here |
|---:|---|---|---:|---|---|---:|---|
| 1 | Jamna Auto Industries Ltd | `JAMNAAUTO` | 5,134 | Quality compounder | Early Stage 2 | 90.8 | 5y median ROCE 27%; 5y revenue CAGR 19%; 5y EPS CAGR 26%; cumulative CFO/PAT 1.37 |
| 2 | Olectra Greentech Ltd | `OLECTRA` | 11,206 | High-growth company | Early Stage 2 | 86.9 | 3y revenue CAGR 28%; 3y EPS CAGR 39%; latest quarter revenue +86% YoY |
| 3 | Chennai Petroleum Corporation Ltd | `CHENNPETRO` | 18,543 | Mature value/yield company | Mature/extended Stage 2 | 86.5 | dividend yield 5.0%; FCF positive in 5 of last 5 years |
| 4 | Kirloskar Pneumatic Company Ltd | `KIRLPNU` | 9,651 | High-growth company | Early Stage 2 | 86.4 | 3y revenue CAGR 59%; 3y EPS CAGR 142%; latest quarter revenue +84% YoY |
| 5 | SKM Egg Products Export (India) Ltd | `SKMEGGPROD` | 1,335 | Quality compounder | Early Stage 2 | 85.5 | 5y median ROCE 30%; 5y revenue CAGR 23%; 5y EPS CAGR 45%; cumulative CFO/PAT 0.94 |
| 6 | Gujarat Pipavav Port Ltd | `GPPL` | 7,277 | Mature value/yield company | Stage 4 decline | 84.7 | dividend yield 3.6%; FCF positive in 5 of last 5 years |
| 7 | Epigral Ltd | `EPIGRAL` | 4,863 | Quality compounder | Early Stage 1 | 83.9 | 5y median ROCE 25%; 5y revenue CAGR 25%; 5y EPS CAGR 26%; cumulative CFO/PAT 1.46 |
| 8 | Sharda Cropchem Ltd | `SHARDACROP` | 7,327 | Mature value/yield company | Stage 3 distribution | 83.8 | dividend yield 1.9%; FCF positive in 5 of last 5 years |
| 9 | Gujarat Narmada Valley Fertilizers & Chemicals Ltd | `GNFC` | 7,981 | Cyclical recovery | Early Stage 2 | 83.3 | OPM 17.0pp below 5y peak; PAT 53% below 5y peak; quarterly OPM +13.0pp YoY - margin inflecting |
| 10 | SEAMEC Ltd | `SEAMECLTD` | 3,846 | High-growth company | Early Stage 2 | 83.1 | 3y revenue CAGR 30%; 3y EPS CAGR 97%; latest quarter revenue +55% YoY |
| 11 | Action Construction Equipment Ltd | `ACE` | 13,242 | Quality compounder | Early Stage 1 | 82.7 | 5y median ROCE 32%; 5y revenue CAGR 22%; 5y EPS CAGR 38%; cumulative CFO/PAT 1.15 |
| 12 | Multi Commodity Exchange of India Ltd | `MCX` | 70,110 | Financial compounder | Early Stage 2 | 81.7 | ROE 56.3%; 5y EPS CAGR 43% |
| 13 | Acutaas Chemicals Ltd | `ACUTAAS` | 26,797 | High-growth company | Early Stage 2 | 81.6 | 3y revenue CAGR 29%; 3y EPS CAGR 56%; latest quarter revenue +42% YoY |
| 14 | TajGVK Hotels & Resorts Ltd | `TAJGVK` | 2,217 | High-growth company | Early Stage 1 | 81.6 | 3y EPS CAGR 64%; latest quarter revenue +49% YoY |
| 15 | Steel Authority of India Ltd | `SAIL` | 71,592 | Cyclical recovery | Early Stage 2 | 81.3 | OPM 10.0pp below 5y peak; PAT 72% below 5y peak; quarterly OPM +5.0pp YoY - margin inflecting |
| 16 | Cemindia Projects Ltd | `CEMPRO` | 21,764 | Quality compounder | Mature/extended Stage 2 | 81.3 | 5y median ROCE 27%; 5y revenue CAGR 30%; 5y EPS CAGR 107%; cumulative CFO/PAT 1.53 |
| 17 | Krishna Defence & Allied Industries Ltd | `KRISHNADEF` | 1,773 | High-growth company | Early Stage 2 | 80.9 | 3y revenue CAGR 56%; 3y EPS CAGR 81%; latest quarter revenue +25% YoY |
| 18 | Petronet LNG Ltd | `PETRONET` | 42,347 | Mature value/yield company | Mature Stage 1 base | 80.8 | dividend yield 3.6%; FCF positive in 5 of last 5 years |
| 19 | Waaree Renewable Technologies Ltd | `WAAREERTL` | 9,503 | High-growth company | Stage 4 decline | 80.7 | 3y revenue CAGR 112%; 3y EPS CAGR 105%; latest quarter revenue +42% YoY |
| 20 | National Aluminium Company Ltd | `NATIONALUM` | 70,287 | Mature value/yield company | Early Stage 2 | 80.3 | dividend yield 3.0%; FCF positive in 4 of last 5 years |

## 8. Market and sector observations

- Of the 987 eligible companies, 22% are in Stage 4 decline and 4% in Stage 3 distribution, while 40% are in Stage 2 advances. This is a market with real internal dispersion rather than a uniform trend - stage selection carries genuine information here.
- 34% sit in Stage 1 basing formations, which is where the brief's preferred entries live; a meaningful share of the candidate list is therefore awaiting confirmation rather than already extended.
- The candidate set concentrates in Not disclosed (57 names). Phase 3 must check that this does not become hidden single-theme exposure in the portfolio.
- 803 screened companies carry at least one classified corporate-event flag from NSE announcements over the trailing 15 months.

## 9. Data gaps and limitations

Stated plainly, because they bound what Phase 2 can rely on:

1. **BSE-only companies were not screened.** The coverage claim is NSE main-board series-EQ only.
2. **Fundamentals are aggregator-sourced (screener.in), which is a secondary source.** It compiles audited filings but is not itself the filing. Every material figure for a shortlisted name must be confirmed against the annual report or exchange filing in Phase 2.
3. **No normalisation of exceptional items.** Reported EPS/PAT only - see section 4.
4. **Net debt is gross borrowings.** Cash is not separately available; leverage is overstated for cash-rich companies.
5. **Promoter pledging was not captured.** The aggregator's shareholding block does not expose pledge percentages. This is a required Phase 2 check, not an optional one.
6. **Auditor qualifications, related-party transactions, contingent liabilities and CWIP ageing were not assessed.** These need the annual report and are Phase 2 work.
7. **Capex evidence is balance-sheet inference.** Capex candidates were identified from CWIP intensity and CWIP drawdown into fixed assets, not from verified commissioning dates or order books. Phase 2 must verify commissioning, funding and demand from filings, presentations and calls before any capacity model is built.
8. **Event flags are keyword-classified from announcement text**, not read in full. False positives are possible; each flagged event must be confirmed against the underlying intimation.
9. **Relative strength uses the Nifty 500 plus one of twelve sector indices.** Companies whose NSE industry label is missing were compared to the broad index only.
10. **Liquidity is a weekly-derived substitute** for a true daily 3-month median.
11. **Forward estimates are absent by design.** No consensus data was available, so all growth figures are realised, not forecast. Phase 2 introduces the forward view.

## 10. Instructions for Phase 2

Validate in this order - these are the issues most likely to change a conclusion:

1. **Confirm market cap and share count** from the exchange, then recompute per-share figures on fully diluted shares. Aggregator market caps drift after corporate actions.
2. **Recompute net debt properly** (borrowings less cash and liquid investments) for every candidate. Several apparent leverage flags in this file will dissolve.
3. **Check promoter pledging and any auditor qualification** for every candidate - neither was available at Phase 1, and either can be disqualifying.
4. **Normalise earnings.** Strip exceptional items and reconcile reported to normalised EPS. Companies carrying an `earnings_quality_flag` are the priority.
5. **For every capex candidate**, replace the CWIP inference with verified commissioning dates, capex spent versus budget, funding, and demand evidence. Reject any name where capacity exists but demand evidence does not.
6. **For every cyclical candidate**, build the mid-cycle margin from the full history and value on that - not on the trough and not on the peak.
7. **For every event-driven candidate**, read the actual scheme or offer document and establish the regulatory path, timeline and no-event downside.
8. **Treat the technical stage as timing information only.** It must not rescue a company whose fundamentals or governance fail. Phase 3 owns the final technical read.
9. Names with `data_quality_confidence = Low` need their data rebuilt from filings before any model is trusted.

## 11. Exclusion-code dictionary

| Code | Meaning |
|---|---|
| `EX_NO_FUNDAMENTALS` | No usable financial record could be retrieved for the symbol (typically a trust, REIT/InvIT, recently suspended scrip, or a symbol with no aggregator page). |
| `EX_NO_MCAP` | Market capitalisation not available from any source used. |
| `EX_MCAP_BELOW_BAND` | Market cap below the INR 1,000 cr floor. |
| `EX_MCAP_ABOVE_BAND` | Market cap above the INR 1,00,000 cr ceiling. |
| `EX_ILLIQUID` | Median daily traded value below INR 1.0 cr over the trailing 13 weeks. |
| `EX_SHORT_FIN_HISTORY` | Fewer than 3 annual reporting periods available. |
| `EX_NO_PRICE_HISTORY` | Fewer than 40 adjusted weekly bars - Weinstein staging impossible. |
| `EX_NO_ARCHETYPE` | No archetype discovery test passed on the available data; the company does not present an identifiable return mechanism at Phase 1 depth. |
| `EX_DATA_QUALITY` | Core screening metrics missing - classification would not be reliable. |

## 12. Files in this hand-off

| File | Rows | Purpose |
|---|---:|---|
| `P1_screened_universe.csv` | 2086 | every security evaluated, including exclusions |
| `P1_candidates.csv` | 150 | the selected set, with Phase 2 questions and required documents |
| `P1_source_log.csv` | 3237 | every source ID referenced in the CSVs |
| `P1_summary.md` | - | this document |
| `P1_run_manifest.json` | - | counts, cutoffs, checksums, resume instructions |

**Units.** All currency fields are Indian rupees. `market_cap_inr_cr` and `liquidity_value_inr_cr` are in crore (1 cr = 10,000,000). Prices are per share in rupees. All `_pct` fields are percentages, not fractions. `net_debt_to_equity` and `cfo_pat_ratio` are ratios. Dates are `YYYY-MM-DD`.

---
*This is analytical research output produced by an automated screen. It is not personalised investment advice.*