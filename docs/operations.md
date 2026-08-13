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
| `SCREENER_PRICE_BASIS` | `yahoo_adjclose` | `yahoo_adjclose` or `split_bonus`. See the cutover note below |
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

## Recovering from a failure

**A killed run.** `screener screen --resume` continues the most recent unfinished
run for that `as_of` at the right stage.

**A stage failed.** The error is in `screen_stage.error` for that run. Fix, then
re-run — completed stages are skipped.

**A parser bug.** Fix the parser and run `screener rebuild-facts`. Raw payloads
are retained precisely so a fix can be replayed across every page already
collected, with no re-fetching and no dependence on the original files.

**Migration drift.** `screener migrate --verify` reports it. A migration edited
after being applied halts the runner rather than layering onto an unknown base;
resolve by hand.

**Stranded retry claims.** A crash mid-claim leaves rows `in_flight`; the lease
timeout returns them to `pending` on the next drain.

## The price-basis cutover

The technical layer runs on `yahoo_adjclose`. Moving to `split_bonus` (bhavcopy,
price return) is now **mechanically possible** — NSE's `ind_close_all_<date>.csv`
supplies price-return series for all twelve benchmark indices, which was the
blocker — but the evidence does not yet justify it. See
[decisions.md F20](decisions.md) for the measurement.

Both bases now run to five years, so history length is no longer the issue. The
remaining blocker is reconciliation: **~306 securities carry corporate actions we
have not found**, and on the price-return basis there is no Yahoo fallback to
demote them to, so they are excluded outright. That costs 162 companies from the
eligible set — 1,106 down to 945.

The precondition is therefore to resolve the non-reconciling securities, not to
collect more history. Progress is visible in:

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
