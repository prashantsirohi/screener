# Operations

## First-time setup

```bash
python -m pip install -e ".[dev]"
cp .env.example .env
screener doctor
screener migrate
```

`doctor` is the diagnostic to reach for first — it reports the Python version,
Postgres reachability, whether the DuckDB `postgres` extension loaded, the
resolved data/report/log roots, and the migration head.

If the DuckDB extension cannot be downloaded (restricted host), set
`SCREENER_ANALYTICS_MODE=parquet` and run `screener export-parquet`; the analytics
SQL is written against the same views either way.

## Configuration

Environment variables, or a `.env` alongside `pyproject.toml`.

| Variable | Default | Notes |
|---|---|---|
| `SCREENER_PG_HOST` / `PORT` / `DATABASE` / `USER` | `127.0.0.1` / `5432` / `market_screener` / `postgres` | |
| `SCREENER_PG_PASSWORD` | unset | Omitted from the conninfo when unset, which suits `trust` auth on loopback |
| `SCREENER_ANALYTICS_MODE` | `attach` | `attach` or `parquet` |
| `SCREENER_PRICE_BASIS` | `split_bonus` | `yahoo_adjclose` or `split_bonus`. See the basis note below |
| `SCREENER_TECHNICAL_GATE` | `default` | `off`, `default`, or a `\|`-separated list of Weinstein stage names to exclude |
| `SCREENER_TECHNICAL_GATE_MIN_RS` | unset | Optional 13-week relative-strength floor, in percent |
| `SCREENER_MIN_SELECT_SCORE` | `60` | Hard candidate score floor, 0-100. See the floor note below |
| `SCREENER_SESSION_COOKIE` | unset | Optional screener.in session cookie. Do not automate login; check their terms |
| `DATA_ROOT` / `REPORTS_ROOT` / `LOGS_ROOT` | `./data`, `./reports`, `./logs` | Move artifacts off the project tree |
| `SCREENER_LOG_LEVEL` | `INFO` | |

Non-secret tuning (market-cap band, liquidity floor, candidate target) lives in
`config.py` so it shows up in diffs. It is folded into `config_hash`, which is
recorded on every run.

## Daily operation

```bash
screener sync --source prices        # incremental from the watermark
screener derive --what all           # actions, adjusted, weekly, reconcile
screener screen                      # Phase 1
```

An unchanged re-run is cheap: `s80` skips on a matching input hash and carries
the previous run's rows forward, so the artifacts are still produced.

### Fundamentals

Do **not** refresh all 2,086 companies on a schedule — that is what provoked the
throttle in the first place. The staleness gate keeps steady state near 25 pages
a day. Work the retry queue on a drip instead:

```bash
screener sync --source fundamentals --retry-queue --max-days 50
```

Three or four runs a day clears a backlog of a few hundred over a few days at a
request rate the site has no reason to throttle.

### Suggested Task Scheduler entries

| When | Command |
|---|---|
| Weekdays 19:30 IST | `screener sync --source prices` then `screener sync --source indices` |
| Weekdays 19:45 IST | `screener derive --what all` |
| Weekdays 20:00 IST | `screener screen` |
| Weekdays 20:15 IST | `screener sync --source announcements` |
| Daily 21:00 IST | `screener sync --source fundamentals --max-days 25` (staleness gate) |
| Every 6 hours | `screener sync --source fundamentals --retry-queue --max-days 50` |
| Weekly | `screener sync --source documents` |
| Weekly | `screener metrics --snapshot` then `screener metrics --strict` |

Prices are gated on publication time, so 19:30 is the earliest a same-day
bhavcopy can be relied on. Index closes publish on the same schedule and reuse
the equity trading calendar, so a known holiday is never probed twice.

The fundamentals refresh and the retry drain are separate on purpose: the first
is the routine staleness-gated trickle (~25 pages/day), the second works the
quarantine backlog. Running the refresh without the gate is what caused the
original throttle.

## Backfilling

```bash
screener sync --source prices --backfill 1100   # ~3 years; roughly 30 minutes
screener derive --what all
```

The walker probes every calendar day including weekends (NSE holds occasional
Saturday and Sunday sessions) and records each answer in the trading calendar, so
the probe cost is paid once. Failure is isolated per date.

After any backfill, re-run `derive --what all`: corporate-action inference,
adjusted prices and weekly bars are all functions of the daily series.

## Monitoring

```bash
screener status                  # counts, watermarks, reconciliation trend, alerts
screener status --verbose        # also show checks that are passing
screener status --strict         # exit non-zero if any alert fires
```

`--strict` is what a scheduled run should use, so a failing check surfaces
instead of scrolling past in a log.

### The reconciliation panel

```
price-source reconciliation (as of 2026-08-13, previous 2026-08-11)
  agree              1747   +128
  drift                57    +15
  disagree            115     -1
  missed_action        35   -141  <- unfound corporate actions
                     1.8% of 1954 compared

  largest unexplained steps
    NYKAA          missed_action  step 83.3%  over 248 weeks
```

`missed_action` is the number to watch: a price step landing on a ratio a split
or bonus actually produces, which one series applied and the other did not. Each
one is an action the pipeline has not found, and those securities fall back to
Yahoo — on the price-return basis they would be excluded outright.

It is deliberately kept separate from `disagree`, which is a series that drifts
apart *without* such a step. That is a data-quality question, not a missing
action, and summing the two makes the alert impossible to reconcile against the
table above it.

When `missed_action` rises:

```bash
screener derive --what actions              # re-read the NSE feed
screener derive --what actions-divergence   # recover using Yahoo as a second opinion
screener derive --what adjusted --what weekly
```

The residual is dominated by **demergers**, which the parser deliberately skips —
splitting value across the resulting entities needs data the feed does not carry.
Those need manual handling or an external source.

### Thresholds

| Check | warn | alert |
|---|---|---|
| `missed_action` share | ≥10% | ≥20% |
| `missed_action` rise between snapshots | ≥25 securities | — |
| Blank fundamentals pages | ≥1 | >100 |
| Retry queue `exhausted` | — | ≥1 |
| Watermark age | >5 days | — |
| QC failures on the latest run | — | ≥1 |

Also worth watching:

- **Retry queue `pending` not falling.** The drip is not running, or the source
  is refusing.
- **`exhausted` rows.** Six attempts failed. Those companies are absent from the
  screen; investigate before trusting a run's coverage.
- **`sync_batch` rows stuck in `running`.** A process died. The next run reaps
  them to `interrupted`.

## Backdated runs

`--as-of` is a real point-in-time boundary, not a label. Every fundamental fact,
announcement, price bar and source-log entry is bounded at midnight IST ending
the as-of day, so a re-run reproduces what was knowable then.

```bash
screener screen --as-of 2026-08-10 --force
```

Expect fewer eligible companies the further back you go — that is the mechanism
working. A run dated 2026-08-10 excludes 307 companies as `EX_NO_FUNDAMENTALS`
because their pages were blank until the retry queue recovered them on the 11th.

The bound differs by source on purpose:

| Source | Bound | Meaning |
|---|---|---|
| Fundamentals | `available_at` | When we held the number. No publication date exists, so scrape time is the conservative choice |
| Announcements | `announced_at` | When the market was told. Backfilled rows share one scrape date, so bounding on `available_at` would show nothing |

**The screen cannot be honestly backtested before the first scrape date.** Facts
carry the timestamp we retrieved them, not the date they were published, so a run
dated before that shows an empty store rather than history. Improving this needs
the results-declaration date from the announcement feed, which the schema already
supports.

## Run cost

A full `screener screen` is roughly a minute, dominated by the technical layer:

| Stage | Time | What it does |
|---|---:|---|
| `s80_phase1_screen` | ~50s | Weinstein features for 2,051 securities, then eligibility, classification and scoring for 2,086 |
| `s85_phase1_outputs` | ~6s | Selection, provenance, five artifacts |
| `s90_summary` | ~2s | |
| `s95_qc` | ~3s | 19 checks |

`s80` was 378s until the fundamentals read was bulk-loaded. It is now bounded by
the weekly-bar scan and the Weinstein arithmetic, not by database round trips.

If it regresses, check first whether something reintroduced a per-security query
inside the universe loop — that is what cost 340 seconds.

## Stage caching

An unchanged re-run reuses completed stages:

```
s80_phase1_screen inputs unchanged - reusing p1-2026-08-13-879bdb76
```

The fingerprint covers the data (row counts, watermarks), the **configuration**
(`config_hash` — every threshold, the price basis, the technical gate, the score
floor) and the **metric model version**. Change any of them and the stage
recomputes.

`MODEL_VERSION` in `domain/metrics.py` must be **bumped by hand** when a formula
changes. A formula edit moves no row count and no timestamp, so without it a
non-forced re-run serves the previous code's answers. This is the one cache input
that is not automatic.

`--force` bypasses the cache entirely.

## Recovering from a failure

**A killed run.** `screener screen --resume` continues the most recent unfinished
run for that `as_of` at the right stage.

**A stage failed.** The error is in `screen_stage.error` for that run. Fix, then
re-run — completed stages are skipped.

**A parser bug.** Fix the parser, then:

```bash
screener reparse-pages --dry-run    # how many pages change
screener reparse-pages              # re-parse retained HTML, checksum-verified
screener rebuild-facts              # re-explode the facts
```

Two steps because they fix different things. `reparse-pages` re-runs the parser
over the retained response body; `rebuild-facts` re-explodes facts from the
parsed payload. A **mapping** fix needs only the second. A **parser** fix needs
both — the payload cannot contain what the parser never extracted.

Pages captured before migration 0017 have no retained HTML and are reported as
unreplayable. They re-capture through the staleness gate at ~25/day; `screener
status` shows the coverage.

**A renamed source row.** `metric_id` is a slug of the aggregator's display
label, so a rename mints a new metric and silently empties the old one.

```bash
screener metrics --snapshot         # record current coverage (run on a schedule)
screener metrics                    # compare the last two snapshots
screener metrics --strict           # exit non-zero on any drift
```

It reports appeared, vanished, unit-changed and coverage-collapsed metrics, and
pairs likely renames (`'Sales' -> 'Revenue'`). If `mapping_version` differs
between the snapshots the output says so — that part of the change is ours, not
the source's.

**Migration drift.** `screener migrate --verify` reports it. A migration edited
after being applied halts the runner rather than layering onto an unknown base;
resolve by hand.

**Stranded retry claims.** A crash mid-claim leaves rows `in_flight`; the lease
timeout returns them to `pending` on the next drain.

## The candidate score floor

Selection is **everything scoring at or above the floor, capped at 150** — not
"top 150". The floor is hard: a weak market yields fewer than 150 rather than
padding the queue to hit a number, so the candidate count carries information.

The default floor is **60**, and `SCREENER_MIN_SELECT_SCORE` overrides it.

Every run reports which constraint bound:

```
bound_by=target   top 150 by preliminary priority score
                  (cut at 67.1; 307 cleared the 60 floor)

bound_by=floor    all 39 eligible names scoring >= 75
                  - the hard floor bound before the 150 target
```

`bound_by=floor` is the interesting state: the market did not offer a full queue
at your quality bar.

### Why 60

It is anchored to a structural break in the score distribution, not to whatever
count it produces. Component means across the eligible set:

| band | n | financial /20 | archetype /25 | catalyst /20 | valuation /15 |
|---|---:|---:|---:|---:|---:|
| 75+ | 39 | 17.6 | 20.8 | 15.5 | 9.9 |
| 70–75 | 60 | 17.3 | 18.8 | 11.7 | 8.8 |
| 67–70 | 52 | 17.1 | 16.6 | 11.6 | 7.5 |
| 64–67 | 51 | 16.3 | 15.7 | 11.2 | 6.9 |
| 60–64 | 105 | 15.4 | 15.1 | 9.0 | 6.8 |
| **<60** | **481** | **12.0** | **11.6** | **6.9** | **3.8** |

From 75 down to 60 the profile degrades smoothly. Below 60 every component falls
away together — valuation plausibility nearly halves. Above 60 is a weak thesis;
below it is the absence of one.

Sensitivity, on the 2026-08-13 run (788 eligible):

| floor | clear it | selected |
|---:|---:|---:|
| 60 | 307 | 150 |
| 65 | 188 | 150 |
| 68 | 136 | 136 |
| 70 | 99 | 99 |
| 75 | 39 | 39 |

At 60 the floor does not bind today — the market genuinely offers 307 names above
the bar — so you still get 150. Raise it if the Phase 2 research queue should be
shorter than the market allows.

`QC17` accepts a short list **only when the count equals the number above the
floor**. Fewer candidates than that means selection dropped something it should
have kept, which is a bug rather than a thin market.

## The technical gate

By default the screen **excludes `Stage 3 distribution` and `Stage 4 decline`
from eligibility**. Those are the two stages Weinstein holds are not ownable
however good the underlying business is, and a company failing the gate is
recorded as `EX_TECHNICAL_STAGE`.

Before the gate existed the stage was worth at most 5 of 100 score points and
barely influenced anything. Measured on the 2026-08-13 run, a Stage 4 name was
9.3% likely to be selected against 18.3% for Early Stage 2 — a nudge, not a
filter. The gate is what makes the technical layer actually decide something.

Effect on that run:

| | ungated | gated |
|---|---:|---:|
| Eligible | 1,092 | **788** |
| Excluded by the gate | — | 304 |
| Candidates unchanged | — | **119 of 150** |
| Score floor at 150 | 68.9 | 67.1 |

The floor barely moved, which is the useful part: the excluded names were not
concentrated at the top, so the gate removes untradeable charts without
sacrificing much fundamental quality.

### Tuning it

```bash
SCREENER_TECHNICAL_GATE=off screener screen --force            # disable entirely
SCREENER_TECHNICAL_GATE='Stage 4 decline' screener screen      # allow Stage 3
SCREENER_TECHNICAL_GATE_MIN_RS=0 screener screen               # add an RS floor
```

Stage names must match `weinstein.STAGES` exactly and are validated at settings
load, so a typo fails `screener doctor` rather than silently screening with no
filter. `QC19` independently asserts the gate excluded somebody and that no
excluded stage leaked into the eligible set — a gate that quietly matches nothing
is the failure mode worth engineering against, not a wrong verdict.

An **optional** relative-strength floor (`min_rs_13w`) is off by default. RS is a
timing signal and Phase 1 feeds research that takes weeks, so a basing name with
mildly negative RS is often exactly what you want to start work on now. Note that
191 of 2,051 securities have no computable RS (fewer than 30 overlapping weeks
with the benchmark); under a configured floor those fail as `EX_WEAK_RS`, because
absent evidence is not evidence of strength.

The gate is part of `config_hash`, so gated and ungated runs are distinguishable
and `runs diff` can attribute the delta. The parity suite pins to gate-off for
the same reason it pins to `yahoo_adjclose`.

## The price basis

**The default is `split_bonus`** — exchange-published prices adjusted for splits
and bonuses, i.e. price return. That is the correct basis for stage analysis (a
Weinstein chart is a price chart) and makes the technical layer primary-sourced
end to end: exchange prices, exchange index benchmarks, exchange corporate
actions.

`yahoo_adjclose` (total return) remains available via `SCREENER_PRICE_BASIS` and
is what the frozen parity baseline was built on, so the parity suite pins to it
explicitly rather than following the default.

### How the cutover was decided

Both objections that previously argued against it have been resolved — history
length (both bases now run to five years) and the size of the trusted universe
(the verdict was over-flagging, see [decisions.md F22 and F23](decisions.md)).
The current measurement:

| | yahoo_adjclose | split_bonus |
|---|---:|---:|
| Securities receiving a stage | 1,954 | **2,051** |
| Eligible | 1,106 | 1,092 |
| Candidates unchanged | — | **146 of 150** |
| Excluded as untrusted | — | 35 |

The price-return basis now has better technical coverage, near-identical
eligibility, and 97% candidate stability. It is also the analytically correct
basis for stage analysis — a Weinstein chart is a price chart — and it comes
from the exchange rather than an aggregator.

The remaining 35 exclusions are **demergers**, which the corporate-actions parser
skips because splitting value across the resulting entities needs data the feed
does not carry.

To reverse it, or to compare the two bases again:

```bash
SCREENER_PRICE_BASIS=yahoo_adjclose screener screen --force
screener runs diff <split_bonus_run> <yahoo_run>
```

Progress on the residual is visible in:

```bash
screener derive --what reconcile     # verdict counts
```

Watch `missed_action` and `disagree`. Each one is a corporate action the
adjustment is missing; `derive --what actions-divergence` recovers some
automatically by using Yahoo as a second opinion, but the residual needs the
NSE feed to improve or manual investigation.

To evaluate a cutover:

```bash
screener derive --what reconcile          # confirm reconciliation is healthy
SCREENER_PRICE_BASIS=split_bonus screener screen --force
screener runs diff <previous_run> <new_run>
```

Read the diff before adopting it. A flip should be explicable by the
total-return-versus-price-return difference — which means it should correlate
with dividend yield. If it does not, something else is moving and the cause needs
finding first.

Never flip securities and benchmarks in separate runs; `load_all_weekly` raises
`BasisIncoherent` rather than producing biased relative strength.

## Testing

```bash
pytest                # everything, ~3.5 minutes
pytest tests/unit     # fast, no database, no network
pytest tests/parity   # the acceptance gate
```

Integration tests create and drop a throwaway database per test, and skip
cleanly when no Postgres is reachable. HTTP tests run against recorded fixtures
with no live network.

If a parity test fails, the port changed an answer. Before adjusting the test,
establish which side is right — the failure is doing its job.
