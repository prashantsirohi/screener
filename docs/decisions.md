# Decisions and findings

Why the system is shaped the way it is, and the bugs that shaped it.

Most entries here exist because something produced a *plausible wrong number*
rather than an error. That is the recurring theme of this codebase: in a data
pipeline over scraped market data, the dangerous failures do not raise.

---

## Decisions

### D1 — Postgres as source of record, DuckDB for analytics

Both reference projects this borrows from use DuckDB alone. Postgres was chosen
for writes because the analytical store needs clean upserts, foreign keys and
concurrent readers, and because DuckDB refuses `ON CONFLICT DO UPDATE` on indexed
columns — a constraint the `market_intel` project worked around with
check-then-insert and one delete-then-reinsert.

The cost is two engines, and therefore the risk of drift. The two-engine contract
in [architecture.md](architecture.md) is the price of the decision, not optional
hygiene.

### D2 — The formula stays in Python

The plan called for the Weinstein features to move into DuckDB SQL. They did not.
Reimplementing pandas' `rolling` and `dropna` semantics in SQL is high-risk for
little gain at this scale, and would have made exact parity nearly impossible to
demonstrate.

DuckDB does the bulk scan and the benchmark join — the parts a columnar engine is
actually good at. The contract's real requirement is *one formula, one engine*,
and that is satisfied. Technical parity came out exact as a direct result.

### D3 — `available_at` in the fundamentals primary key

The single decision that makes the store point-in-time. A restatement scraped
later coexists with the figure as it was known before.

Caveat worth stating: `available_at` is currently **scrape** time, not
publication time. That is conservative — it never claims to have known something
early — but it means the screen cannot be honestly backtested before the first
scrape date. The improvement path is to derive it from the announcement feed's
results-declaration date, which the schema already supports.

### D4 — Retain raw payloads

Every scraped page is stored whole. This looked like belt-and-braces until two
parser bugs (D9, D10) needed replaying across 1,843 pages; `rebuild-facts` did it
in seconds with no re-fetching. Payload retention is what makes a parser fix
cheap instead of a re-scrape.

### D5 — One return basis per run

See F1. Enforced in config and re-checked at load.

### D6 — Reconciliation drives source election, on read

Where a bhavcopy-derived history fails to reconcile, Yahoo is demonstrably the
better-adjusted series — it is carrying an action our adjustment is missing.
Rather than ship a history known to be wrong, those securities fall back.

The election is **derived on read** from bar coverage plus the latest verdict.
An earlier version wrote the demotion into `source_rank`, and reloading
`weekly_bar` silently re-promoted 44,669 bars from histories known not to
reconcile. Derived decisions do not belong in mutable data.

### D7 — Parity as the acceptance gate

A frozen pre-rewrite baseline is the oracle. Where an output legitimately
changed, the tests name the cause and assert nothing else moved — rather than
loosening a tolerance until it passes.

Two examples of holding that line:

- Excluding partial weeks made the port end a week earlier than the oracle. The
  fix was to align the oracle's bar set, so parity still measures the *port*,
  with the partial-week policy tested separately.
- A single residual mismatch (one company's overhead supply, 78.9 vs 78.6) traced
  to `numeric(18,6)` truncating float32 noise and flipping a `typical > price`
  comparison. Widening to `numeric(24,10)` removed the whole class of knife-edge
  divergence rather than tolerancing the one instance.

### D8 — The Weinstein stage gates eligibility, it does not just score

Axis C began as 5 of 100 score points. Measured on the 2026-08-13 run, that made
it almost inert: a `Stage 4 decline` name was 9.3% likely to be selected against
18.3% for `Early Stage 2`, and `Mature Stage 1 base` — which the score *rewards*
with 4 of 5 stage points — had the lowest selection rate of any stage at 4.7%,
because the stage bonus was swamped by the 90 fundamental points.

Comparing the six components between the selected 150 and the other 942 eligible
made the scale of it plain:

| component | max | candidates | rest | gap |
|---|---:|---:|---:|---:|
| archetype evidence | 25 | 19.3 | 12.8 | 6.5 |
| catalyst visibility | 20 | 13.1 | 7.4 | 5.7 |
| valuation plausibility | 15 | 9.4 | 5.0 | 4.4 |
| financial quality | 20 | 17.6 | 13.2 | 4.3 |
| governance | 10 | 9.0 | 7.7 | 1.3 |
| **technical & liquidity** | **10** | **5.9** | **5.3** | **0.6** |

The most heavily engineered part of the system — five years of exchange bars,
30/40-week MAs, base detection, relative strength against sector and broad index
— moved the ranking by 0.6 points out of 100.

So `Stage 3 distribution` and `Stage 4 decline` now fail eligibility outright,
recorded as `EX_TECHNICAL_STAGE`. Three design points:

- **The gate runs last.** Gates short-circuit and the first failure is the
  recorded code, so inserting it earlier would relabel exclusions whose meaning
  had not changed. An illiquid Stage 4 company is still `EX_ILLIQUID`.
- **It defaults off in `assess()`.** The four-argument form is byte-identical to
  the frozen oracle, so the parity suite keeps testing the port rather than the
  gate — the same discipline as the basis pin in D5.
- **Stage names are validated at settings load.** A typo would match nothing,
  and the run would look healthy with the filter simply absent. That is the same
  shape as a QC check that cannot fail (F14), so `QC19` independently asserts the
  gate excluded somebody and that no excluded stage leaked through.

Effect: eligible 1,092 → 788, 119 of 150 candidates unchanged, score floor
68.9 → 67.1. The floor barely moving is the reassuring part — the gate removed
untradeable charts without sacrificing fundamental quality.

The relative-strength floor is built but **off by default**. RS is a timing
signal and Phase 1 feeds research measured in weeks; a basing name with mildly
negative RS is often exactly what should be researched now.

---

## Findings

Each of these shipped, and each produced wrong numbers rather than an error.

### F1 — Mixed return bases inside one price history

**P0.** Per-week source election spliced Yahoo total-return and bhavcopy
price-return bars into a single series for **1,406 of 2,086** active securities,
across 3,024 transitions. At every seam the series steps by the cumulative
dividend yield, so a 30- or 40-week moving average spanning it averages two
different quantities — capable of creating or destroying a stage transition
outright.

Fixed at three levels: one source per security in the resolved view, a run-level
basis pin, and a load-time check that raises if either invariant breaks.

### F2 — Reconciliation corrupted by a many-to-many join

**P0.** The recent-weeks CTE joined on the floating-point *ratio* instead of the
week. Equal ratios in different weeks matched each other: 1,757 of 1,954
securities reported an inflated `weeks_compared`, one 158-week series claiming
23,720 comparisons. Every agree/disagree verdict was untrustworthy.

### F3 — `prev_close` assumption was backwards

The corporate-action inference was built on the premise that NSE restates
`PRVSCLSGPRIC` on an ex-date. It does not — across 269 confirmed gaps the ratio
was exactly 1.000 every time. The signal is close-to-close. The rewritten
inference finds 206 actions clustering on exactly the ratios real splits produce.

### F4 — NSE trades on weekends

A weekday-only date walker dropped real sessions. Four out of four probed weekend
dates had a published bhavcopy. Missing sessions hole weekly bars and make the
gap look like a split — 46 phantom "consolidations" on 2024-01-23 came from
exactly this.

### F5 — A split applied twice

SPORTKING's 1:10 was recorded at 2024-09-09 by the divergence method and
2024-09-13 by the NSE feed. Four days apart, so a ±3-day supersede window missed
it, and `0.1 × 0.1 = 0.01` was applied to every prior bar — the whole history out
by 10×. Fixed with a ±7-day window plus a defensive dedup inside the adjustment
itself, so no source combination can reintroduce it.

### F6 — A preference-share bonus treated as an equity action

*"Scheme Of Arrangement - Bonus Ncrps 4:1"* is a bonus of preference shares and
does not touch the equity price. Parsed as a 4:1 equity bonus, it would have
rescaled TVS Motor's entire history by 0.2.

### F7 — Bank labels aliased away

`Revenue` and `Financing Profit` were mapped onto `Sales` and `Operating Profit`
for comparability. But `is_financial()` detects a lender *by the presence of that
row* — the aliasing would have silently reclassified every bank and NBFC, then
applied CFO/PAT and debt-to-equity tests that are meaningless for them. Caught by
an EAV round-trip test.

### F8 — Non-standard reporting periods dropped

`Mar 2023 15m` (a fiscal-year-change transition period) and `Jul 2026`
(shareholding at a non-quarter month end) did not match the period regex. 1,116
facts recovered.

### F9 — The growth block was never stored

`screener_sales_cagr_5y`, `stock_cagr_5y` and friends come from the
compounded-growth tables, which the fact loader skipped entirely. The round-trip
test had not looked at that block, so it passed while the data was missing.

### F10 — Nulls dropped on load

A reported-but-blank period is not the same as an absent one: `series[-1]`
returns the last *listed* value, so a blank latest year silently became the last
non-blank one, moving `opm_latest_pct` and `inventory_days`. F9 and F10 together
took the fact count from 913k to over 1.0M.

### F11 — Announcement text concatenated in the wrong order

The legacy blob was `attchmntText + desc`; the port used the reverse. Several
patterns are proximity-bounded (`\bsebi\b.{0,60}(order|penalt|…)`), so reversing
the halves lost matches straddling the boundary — 39 "Regulatory action" flags.

### F12 — 1,649 orphaned announcements

Imported before the backfill registered their securities, so `security_id` was
NULL and they vanished from every event query. The 59 symbols still unlinked
predate the price history entirely; a test asserts that is the only reason
anything is unlinked.

### F13 — Queue claims never committed

`claim()` used a read helper that opens a non-autocommit connection and never
commits, so the `in_flight` marking **rolled back entirely**. The claim was
decorative and two runs could genuinely double-fetch. Found by tests written for
a different bug (the missing lease timeout).

### F14 — Turnover never ingested

The original loader renamed two bhavcopy columns that do not exist, so turnover
was always NULL and the liquidity gate was entirely Yahoo-derived. A
required-column assertion now fires loudly.

### F15 — Weekly alignment would have nulled relative strength

Yahoo stamps weekly bars at week *start*; a bhavcopy resample lands on week end.
Joining on mismatched dates drops nearly every row and the RS function returns
`None` rather than erroring.

### F16 — Partial weeks dated into the future

A Monday cutoff produced 4,052 bars stamped the coming Friday, holding two
sessions but compared as complete — dragging the latest MA point and the 52-week
high.

### F17 — `is_active` did not mean active

The three-year backfill registered 427 securities that traded historically but
are not in today's `EQUITY_L`. Without an active flag they widened the screening
universe from 2,086 to 2,513.

### F18 — QC checks that could not fail

Two checks queried a table nothing ever wrote, so they passed vacuously. The
screen now persists the diagnostic metrics they audit, and the cyclical check
reports meaningfully.

### F20 — The price-return cutover is possible but not yet justified

The blocker was that benchmark indices existed only on the Yahoo (total-return)
basis. NSE's `ind_close_all_<DDMMYYYY>.csv` turns out to publish OHLC for ~163
indices, which is price return by construction — so all twelve benchmarks now
have `split_bonus` series and the cutover is mechanically available.

Measured twice. The first comparison, on three years of bhavcopy, showed two
problems: the stage flips did not track dividend yield (flipped names had a
*median dividend yield of 0.000* against 0.180 for unflipped), and the history
was short — 157 weeks against a base-detection lookback of up to 130.

Backfilling bhavcopy and the index closes to five years removed the second
objection entirely and sharpened the first:

| | yahoo_adjclose | split_bonus |
|---|---|---|
| Longest history | 261 weeks | **261 weeks** |
| Securities with ≥40 weeks of bars | 1,930 | **2,135** |
| Securities that actually get a stage | **1,954** | 1,665 |
| Eligible | **1,106** | 945 |
| Candidates unchanged | — | 127 of 150 |

The raw data is now better on the price-return basis — longer coverage, more
securities. The *screen* is worse, and the reason is F21's guard: 425 securities
whose bhavcopy history fails reconciliation are excluded outright, because on
this basis there is no Yahoo fallback to demote to. They drop to
`EX_NO_PRICE_HISTORY`, taking 162 companies out of the eligible set — 3M India,
Abbott India and Chennai Petroleum among them.

So the binding constraint was never history length. It is that ~306 securities
still carry corporate actions we have not found, and the price-return basis has
no way to route around them. Adopting the cutover today would trade 15% of the
eligible universe for a more correct basis on the remainder.

The default stays `yahoo_adjclose`. The real precondition is resolving the
non-reconciling securities, not more history. Recorded in
[operations.md](operations.md).

What the work did buy: the blocker is gone, both bases now run to five years,
bhavcopy covers more securities than Yahoo, and the guard below exists because
of it.

### F23 — `disagree` was also measuring dividend yield, and the cutover verdict reversed

Correcting F22 changed the source election enough to be worth re-running the
cutover comparison. It exposed the same class of error in the other verdict.

The distrust guard demoted both `missed_action` and `disagree`. Broken out, the
verdicts separate cleanly — and not by data quality:

| verdict | n | median div yield | series diff | shows a ≥10% step |
|---|---:|---:|---:|---|
| agree | 1,747 | 0.13% | 1.2% | — |
| **disagree** | 115 | **1.69%** | 5.6% | **0 of 115** |
| drift | 57 | 1.97% | 6.4% | — |
| missed_action | 35 | 0.00% | 21.8% | yes, on a clean ratio |

`disagree` is dividend yield. PTC at 12.9%, Coal India 5.2%, Wipro 6.0%. Not one
of the 115 shows a step any corporate action would produce — they simply diverge
gradually, which is precisely what a total-return series does against a
price-return one. Their bhavcopy history is sound; it is the *comparison* that
differs, by construction.

Demoting them discarded 81 good exchange-sourced histories, and on the
price-return basis excluded those companies from the screen entirely — which is
what made the cutover look expensive.

**With only `missed_action` demoted, the cutover verdict reverses:**

| | yahoo_adjclose | split_bonus |
|---|---:|---:|
| Securities receiving a stage | 1,954 | **2,051** |
| Eligible | 1,106 | 1,092 |
| Candidates unchanged | — | **146 of 150** |
| Securities excluded as untrusted | — | 35 |

Progression across the three measurements: 127 of 150 candidates unchanged, then
132, now 146. Eligibility went from −162 to −14. The price-return basis now has
*better* technical coverage than Yahoo.

The 117 stage flips are explicable. 53 are coverage differences — one basis has
40 weeks where the other does not. The rest sit at threshold boundaries, where a
small cumulative dividend difference tips a moving-average slope across the ±1.5%
flat band. At the latest bar the two series are identical to 0.0000%.

**The default is now `split_bonus`.** The technical layer is primary-sourced end
to end — exchange prices, exchange index benchmarks, exchange corporate actions —
and runs on the basis stage analysis actually calls for.

Two consequences worth holding on to:

* **The parity suite pins to `yahoo_adjclose` explicitly.** The frozen baseline
  predates the rewrite and was built on that basis, and the suite exists to prove
  the *port* changed no answers. Following the default would conflate that with
  the basis question. `test_phase1_row_parity` constructs the baseline-basis
  config hash and matches on it; producing a run on the other basis is a
  documented prerequisite rather than an accident.
* **35 demerger-affected securities still fall back to Yahoo.** They are the
  residual, not a silent gap — `screener status` counts them.

### F21 — On the price-return basis, distrusted series had no fallback

Reconciliation demotes a security whose bhavcopy history fails to reconcile, and
`weekly_bar_resolved` serves Yahoo instead. But a run pinned to `split_bonus`
filters to that basis *before* electing a source, so the Yahoo fallback is not
available — those 344 securities were silently served the very series
reconciliation had already concluded was wrong, and 20 of them reached the
candidate list.

`weekly_series.sql` now excludes them outright on that basis. The company drops
out visibly as `EX_NO_PRICE_HISTORY` rather than carrying a plausible but wrong
stage. Having no series is better than having a known-bad one.

### F22 — The reconciliation verdict over-flagged by 9x

Adding a `missed_action` alert to `status` exposed that the metric it reports was
mostly wrong. The verdict classified *any* ratio step above 10% as a missing
corporate action. But a special dividend or a rights issue moves Yahoo's
total-return series and not the price-return one, by an untidy amount — measured
across the universe, 517 of 566 large steps implied no clean ratio at all.

Requiring the step to land on a ratio a real action produces took the count from
**304 to 87**. Then a test caught the second half of the problem: the ratio table
had been built by inverting every entry, which put 1.2, 1.25, 1.333 and 1.5 in
it. Those are not corporate actions — no consolidation raises a price by half —
and they blanket exactly the band where dividend divergence lives. A selectivity
test showed 24 of 34 arbitrary values matching. Splits and bonuses always reduce
the price; consolidations only occur at whole-number ratios.

| | missed_action | agree |
|---|---|---|
| Original: any step ≥10% | 304 | 1,496 |
| Step must snap to an action ratio | 87 | 1,697 |
| Bonus inverses removed | **35** | **1,747** |

The residual 35 are dominated by **demergers** — Siemens, Raymond, Nykaa — which
the corporate-actions parser deliberately skips, because splitting value across
the resulting entities needs data the feed does not carry.

The lesson is about alerts specifically: an alert is only as good as the metric
behind it. Wiring one up is what forced the metric to be examined, and the metric
was wrong by a factor of nine.

### F19 — A skipped stage left the run empty

`s80` correctly skipped on an unchanged input hash, and `s85` then failed on a
missing column — it reads by `run_id` and nothing had been written. A skipped
stage must still make its output available to the current run, or self-contained
runs and run-to-run diffing do not work.

---

## Open items

- **`available_at` is scrape time.** See D3.
- **Reconciliation sits at 81% agreement.** The residual is flagged per security
  rather than silent, and recent weeks — what current screening reads — agree
  essentially perfectly.
- **NSE `quote-equity` returns 403** to unauthenticated clients, so the
  independent share-count source is unavailable. Market-cap coverage reached
  2,085/2,086 through the retry queue instead, making it a redundancy rather than
  a dependency.
- **35 demerger-affected securities** still fall back to Yahoo, because splitting
  value across the resulting entities needs data the NSE feed does not carry.
- **191 of 2,051 securities have no computable relative strength** (fewer than 30
  overlapping weeks with the benchmark). Immaterial today because the RS floor is
  off; turning it on would exclude them as `EX_WEAK_RS`.
- **Phases 2 and 3 are not built.**
