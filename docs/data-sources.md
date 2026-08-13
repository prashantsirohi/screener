# Data sources

Every source, what it is trusted for, and the quirks it turned out to have. The
quirks section is the useful part: each one was found by inspecting data, not by
anything failing.

## Summary

| Source | Role | Basis | Watermark |
|---|---|---|---|
| NSE `EQUITY_L` | Universe definition | Primary | file date |
| NSE bhavcopy | Daily OHLC, volume, turnover | Primary | `MAX(trade_date)` |
| NSE index constituent files | Industry classification | Primary | snapshot date |
| NSE corporate actions | Splits and bonuses | Primary | `MAX(ex_date)` |
| NSE index closes | Price-return benchmark series | Primary | `MAX(trade_date)` |
| NSE corporate announcements | Events, governance flags | Primary | `MAX(announced_at)` |
| screener.in | Fundamentals | Secondary | per-security `fetched_at` |
| Yahoo Finance | Weekly bars, benchmark indices | Secondary | per-security `MAX(week_end_date)` |

Primary means the exchange or the company said it. Secondary means an aggregator
compiled it. Fundamentals are secondary throughout — adequate for discovery,
never sufficient for a conclusion.

---

## NSE bhavcopy

The source of record for prices, and the only source that carries **turnover**,
which the liquidity gate needs.

**Two layouts.** NSE replaced the legacy `cm<DDMMMYYYY>bhav.csv.zip` file with
UDiFF during 2024. Both readers are implemented and the fetcher probes rather
than hardcoding a cutover date, caching per year which one worked.

**Column names differ between them.** UDiFF uses `TTLTRADGVOL` (volume) and
`TTLTRFVAL` (turnover). A required-column assertion fires if either goes missing,
because the original pipeline silently renamed two columns that do not exist and
carried a NULL turnover column for its entire life without noticing.

### Quirk: NSE trades on some weekends

A date walker that skips Saturday and Sunday loses real sessions. Confirmed
published bhavcopies:

| Date | Day | Occasion |
|---|---|---|
| 2023-11-12 | Sun | Diwali Muhurat |
| 2024-01-20 | Sat | Special session |
| 2024-03-02 | Sat | Special session |
| 2024-05-18 | Sat | Special session |
| 2025-02-01 | Sat | Union Budget |
| 2026-02-01 | Sun | Union Budget |

Four out of four probed dates had data on the first attempt — the phenomenon is
far more common than it looks. A missing session holes the weekly bar for that
week and makes the corporate-action inference read the gap as a split; that is
what produced 46 phantom "consolidations" on 2024-01-23.

Every calendar day is now probed. The trading calendar caches each answer, so the
cost is paid once.

### Quirk: a missing bhavcopy is not always a holiday

The full file publishes well after the 15:30 close. Before roughly 19:00 IST a
missing file means "not published yet", and recording it as a holiday would
exclude that session from every future sync — the calendar is consulted before
the fetch. `_is_settled()` gates this.

### Quirk: `PRVSCLSGPRIC` is the raw prior close

It is **not** restated onto the post-action basis. Measured across 269 confirmed
large gaps, `prev_close / prior_close` was exactly 1.000 in every case. The split
signal is therefore in the close-to-close ratio, not in `prev_close`.

---

## NSE corporate actions

The authoritative source for splits and bonuses. Free-text `subject` is parsed
into an adjustment factor:

```
"Face Value Split (Sub-Division) - From Rs 10/- To Rs 1/-"  -> 0.1
"Bonus 1:1"                                                 -> 0.5
"Bonus 3:5"                                                 -> 0.625
```

A bonus of `a:b` means `a` new shares per `b` held, so the count goes `b → a+b`
and the price factor is `b/(a+b)`.

**Quirk: "Re" versus "Rs".** NSE writes the singular "Re 1/-" for one rupee. A
regex matching only `rs\.?` silently drops those.

**Quirk: bonuses of other instrument classes.** *"Scheme Of Arrangement - Bonus
Ncrps 4:1"* is a bonus of non-convertible redeemable **preference shares**. It
does not change the equity share count and must not adjust the equity price —
parsing it as a 4:1 equity bonus would rescale that company's entire history by
0.2. Subjects mentioning NCRPS, preference shares, debentures or warrants are
excluded.

Rights issues are ignored: pricing them needs a subscription price this feed does
not carry.

### Inference as a secondary check

Price gaps supply actions the feed misses, with three guards:

- **Gap safety.** `LAG()` reaches across holes in the history. During a partial
  backfill this compared a 2023 price to a 2026 one and manufactured 1,761
  phantom events. Only genuinely consecutive sessions are compared.
- **Cluster rejection.** A real action affects one company. When many unrelated
  securities show the same discontinuity on one date, the cause is a missing
  session, not simultaneous splits.
- **Ratio snapping.** A split lands on a simple fraction; a crash does not.
  Candidates that do not sit close to one are rejected.

Price alone still cannot separate a shallow bonus (factor 0.75) from a 28% bad
day. Those are recorded as `unconfirmed` and **excluded from adjustment** until
the feed corroborates them.

---

## screener.in

Fundamentals: nine-plus years of P&L, balance sheet, cash flow, ratios,
quarterly figures, shareholding and compounded-growth tables.

### Quirk: throttling returns HTTP 200 with an empty page

Under sustained scraping, screener serves the full page skeleton with every
numeric span emptied. Nothing at the transport layer distinguishes it from a good
page — 307 of 2,086 companies came back this way, silently excluding real
businesses (Abbott India among them) from the screen.

Detection is structural and three-layered: HTTP status, then a WAF/interstitial
content check, then a **numeric-emptiness** test — all top-ratio spans empty
*and* every statement value null. A genuinely tiny company still reports some
number, so the test has no false positives across the 2,086-company cache.

Blanks are quarantined and queued. Recovery needs a **fresh session and a forced
warmup per symbol**, not merely a longer delay: a uniform 3-second retry
recovered 0 of 20, while session-per-symbol recovered 6 of 6 and ultimately 306
of 307.

### Quirk: lenders use different row labels

Banks and NBFCs report `Revenue`, `Financing Profit` and `Financing Margin %`
where other companies report `Sales`, `Operating Profit` and `OPM %`. These must
**not** be aliased together: `is_financial()` detects a lender by the presence of
a `Financing Profit` row, so folding the labels would silently reclassify every
bank and then apply CFO/PAT and debt-to-equity tests that mean nothing for them.

### Quirk: non-standard reporting periods

Two forms appear that a `Mar|Jun|Sep|Dec` regex drops:

- **`Mar 2023 15m`** — a transition period after a fiscal-year change. Dropping
  it loses a whole year of P&L; treating it as 12 months overstates growth. The
  duration is carried in `period_type` (`annual_15m`) so it can be excluded from
  CAGR arithmetic explicitly.
- **`Jul 2026`** — shareholding filed at a non-quarter month end.

Together these accounted for 1,116 lost facts.

### Quirk: nulls carry information

A period reported blank is not the same as a period that is absent — `series[-1]`
returns the last *listed* value, so dropping nulls silently turns a blank latest
year into the last non-blank one. Nulls are stored.

### Market cap

The aggregator's figure is used where present. The fallback —
`close × (equity capital ÷ face value)` — reads equity capital from the *same
page*, so it cannot rescue a blank shell. It is a cross-check, not the blank-page
fix; the retry queue is that. NSE's `api/quote-equity` (`securityInfo.issuedSize`)
would be an independent source but answers **403** to unauthenticated clients
regardless of pacing.

---

## NSE index closes

`ind_close_all_<DDMMYYYY>.csv` publishes OHLC, volume and turnover for ~163
indices. Twelve are tracked, mapped from NSE's display name ("Nifty 500") to the
store's symbol (`NIFTY_500`) on a normalised key so spacing and casing changes do
not break the mapping.

This is the source that unblocks a price-return technical layer: the series is
price return by construction, where Yahoo's is total return. Indices flow through
the same machinery as equities — no corporate actions, so the adjustment factor
stays 1.0 — and the resample yields `split_bonus` benchmarks.

The sync reuses the equity trading calendar rather than probing dates itself:
the exchange publishes both files on the same sessions.

---

## Yahoo Finance

Weekly adjusted bars, and until the index-close collector existed, the only
source of benchmark series. Still the default basis: it carries 261 weeks of
history against bhavcopy's 157, which matters for a 130-week base lookback.

**Quirk: weekly bars are stamped at week *start*.** Yahoo dates a weekly bar on
the Monday; a bhavcopy resample lands on the week end. Joining a stock to a
benchmark on mismatched dates drops nearly every row, and the relative-strength
function returns `None` rather than erroring — a silent wrong answer. Every bar
is normalised to the ISO week's Friday at load.

**Quirk: the v7 quote endpoint is gated.** Only the v8 chart endpoint works
without a crumb.

---

## Return bases do not mix

Yahoo's `adjclose` is **total return** — it strips dividends. A bhavcopy series
adjusted for splits and bonuses is **price return**. The two diverge steadily
going back: measured at ~1.3% over three years, and 0.0% on the most recent week
where both bases coincide.

Splicing them inside one lookback steps the series by the cumulative yield, and a
30- or 40-week moving average spanning the seam averages two different
quantities. Before this was fixed, 1,406 of 2,086 active securities carried both.

`SCREENER_PRICE_BASIS` pins the whole run to one basis and the loader refuses to
proceed if more than one appears.

## Reconciliation

Both weekly series are retained and compared per security. What that can and
cannot test:

- **Recent agreement is meaningful.** Both bases coincide at the latest bar, so
  recent weeks must match tightly — all 1,954 securities agree within 0.1%.
- **Historical divergence is expected**, not an error. A blanket "95% of all
  weeks within 1%" test would condemn every dividend payer.
- **A step in the ratio is the real signal.** Dividends produce gradual drift; a
  missed corporate action produces a sudden jump.

The verdict drives source election: `agree` and `drift` keep bhavcopy, while
`missed_action` and `disagree` fall back to Yahoo — 344 securities currently. The
election is derived on read from the latest verdict, so a reload cannot lose it.

## HTTP discipline

All collectors share one client: browser-like headers, a cookie warmup (NSE
answers 503 to a cold request), a 1.5-second minimum gap with jitter, urllib3
adapter retries plus an application-level retry that re-warms the session, and a
`Temporary`/`Permanent`/`BlankPage` error taxonomy. Failure is isolated per item —
one bad day or symbol is an error row, not an aborted run.

Note that a heavy client-rendered page can hang a plain GET until it times out
(`/get-quotes/equity` does), so the Referer header and the warmup navigation are
chosen separately rather than reusing one URL for both.
