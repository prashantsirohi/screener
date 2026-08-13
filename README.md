# market-screener

A persistent, reproducible screening system for Indian listed equities.

Syncs NSE market data and company fundamentals into Postgres, then runs a
Weinstein-stage and archetype screen over the result. Every run is versioned,
auditable, and diffable against the last.

> Analytical research tooling. **Not investment advice**, and not a
> recommendation to buy or sell anything.

---

## What it does

```
sync  →  NSE bhavcopy, corporate actions, announcements, screener.in fundamentals
          ↓  (Postgres: point-in-time facts, append-only prices, hash-deduped events)
screen →  eligibility → three-axis classification → 0-100 priority score
          ↓
output →  P1_screened_universe.csv, P1_candidates.csv, P1_source_log.csv,
          P1_summary.md, P1_run_manifest.json
```

The screen classifies each company on three independent axes:

| Axis | What it assigns |
|---|---|
| **A** | Exactly one of ten investment archetypes (quality compounder, capex operating leverage, cyclical recovery, event-driven, …) |
| **B** | Secondary tags from a controlled vocabulary (peak-cycle risk, capex commissioning, governance risk, …) |
| **C** | A Weinstein technical stage, computed arithmetically from adjusted weekly bars — never by judgement |

## Current state

Phase 1 is complete and passes **212 tests**. Phases 2 (forensic validation and
valuation) and 3 (technical confirmation and portfolio construction) are not
built; the schema and point-in-time design do not preclude them.

A representative run:

```
run p1-2026-08-11-ea35f982 -> complete
  evaluated  2086      eligible  1106      selected  150
  s80_phase1_screen   complete   with_technicals=1954
  s85_phase1_outputs  complete   source_records=4979
  s90_summary         complete   files=5
  s95_qc              complete   passed=18, total=18
```

What is in the store:

| | |
|---|---|
| Active NSE series-EQ securities | 2,086 |
| Daily price bars (1,243 sessions since 2021-08, with turnover) | 2,426,758 |
| Weekly bars (both sources retained) | 962,748 |
| Point-in-time fundamental facts | 1,151,022 |
| Corporate announcements | 227,498 |
| Corporate actions | 713 |
| Primary documents discovered | 246 |

## Quickstart

Requires Python 3.11+ and a reachable PostgreSQL 14+.

```bash
git clone https://github.com/prashantsirohi/screener.git
cd screener
python -m pip install -e ".[dev]"
cp .env.example .env          # adjust connection details if needed

screener doctor               # environment, Postgres, DuckDB extension, paths
screener migrate              # creates the database and applies the schema
```

Then populate and run:

```bash
screener sync --source prices  --backfill 1830  # ~5 years of bhavcopy
screener sync --source indices --backfill 1830  # price-return benchmark series
screener sync --source announcements --backfill 450
screener derive --what all                      # corporate actions, adjusted, weekly
screener classify-events                        # taxonomy over announcements
screener screen                                 # Phase 1 -> reports/runs/<run_id>/output/
```

`screener status` shows row counts, per-source watermarks and the retry-queue
depth at any point.

## Command reference

| Command | Purpose |
|---|---|
| `doctor` | Verify Python, Postgres, DuckDB extension, paths, migration head |
| `migrate [--verify] [--target]` | Apply schema migrations; `--verify` reports drift without applying |
| `status` | Row counts, watermarks, retry-queue depth, quarantined pages |
| `sync --source {prices,indices,announcements,documents,fundamentals,shares}` | Incremental refresh. `--backfill N`, `--max-days N`, `--force`, `--retry-queue` |
| `derive --what {actions,actions-divergence,adjusted,weekly,reconcile,all}` | Rebuild corporate actions, adjusted prices, weekly bars, source reconciliation |
| `classify-events [--diff]` | Classify announcements under both taxonomy versions |
| `rebuild-facts` | Re-explode facts from retained page payloads after a parser fix — no re-fetching |
| `screen [--as-of] [--force] [--resume] [--stages]` | Run Phase 1 and emit the hand-off |
| `runs list \| show <id> \| diff <a> <b> \| prune` | Inspect and compare runs |
| `export-parquet` | Materialise the source tables for the offline analytics mode |
| `import-legacy-cache` | One-off import of the original JSON caches |

`runs diff` is the payoff of retaining every run in full:

```
diff p1-2026-08-11-29f6a1fa  ->  p1-2026-08-11-faef83d3
universe    in both 2086, only base 0, only other 0
eligible    1106 -> 1097  (+2 / -11)
candidates  150 -> 150  (entered 4, left 4, unchanged 146)
  entered: AJAXENGG, BDL, BHARATSE, BSOFT
  left   : GRAUWEIL, JAGRAN, KRISHNADEF, SUPREMEIND

fields changed for companies in both runs
  liquidity_value_inr_cr    595   3MINDIA: 12.9950 -> 13.1830
  technical_stage           151   AAATECH: Mature Stage 1 base -> Stage 3 distribution
  preliminary_priority_score 61   ARFIN: 42.70 -> 40.70
```

## Design in brief

**Postgres is the source of record; DuckDB only reads.** Postgres owns every
write — real parameterised upserts, identity columns, foreign keys. DuckDB
attaches read-only for bulk scans and joins, and results come back as Arrow to be
written by psycopg. One writer, always.

**A formula lives in exactly one engine.** DuckDB does the scan and the benchmark
join; the Weinstein arithmetic lives in Python and only there. A formula
implemented twice eventually produces two answers and no test tells you which is
right.

**Point-in-time by construction.** `screener_fact` carries `available_at` in its
primary key, so a restatement never overwrites what was known before, and an
as-of query cannot see a number that had not been published yet.

**One return basis per run.** Yahoo's adjusted close is total return; a bhavcopy
series adjusted for splits and bonuses is price return. Mixing them inside one
lookback steps the series by the cumulative dividend yield and corrupts every
moving average spanning the seam.

**Raw payloads are retained.** A parser fix can be replayed over every page
already collected (`rebuild-facts`) without re-fetching anything.

See [docs/architecture.md](docs/architecture.md) for the schema and stage
pipeline, and [docs/decisions.md](docs/decisions.md) for why each of these was
chosen — including the bugs that forced them.

## Data sources

| Source | Role | Basis |
|---|---|---|
| NSE `EQUITY_L` | Universe definition | Primary |
| NSE bhavcopy (UDiFF and legacy) | Daily OHLC, volume, **turnover** | Primary |
| NSE corporate actions feed | Splits and bonuses | Primary |
| NSE corporate announcements | Event classification, governance flags | Primary |
| screener.in company pages | Fundamentals | Secondary |
| Yahoo Finance | Weekly bars and benchmark indices | Secondary |

Fundamentals are aggregator-sourced and treated as secondary throughout: adequate
for discovery, but anything material must be confirmed against the filing itself.
Full detail, including the quirks each source turned out to have, is in
[docs/data-sources.md](docs/data-sources.md).

## Testing

```bash
pytest                    # everything
pytest tests/unit         # no database, no network
pytest tests/parity       # the acceptance gate
```

The parity suite is the point. A frozen pre-rewrite baseline lives in
`phase1_baseline_20260810/`, and the tests assert the rebuilt system reproduces
it: metrics field-for-field, technicals within `1e-6`, archetypes and priority
scores exactly. Where an output legitimately changed, the difference must be
*attributable* — the tests name the cause and assert nothing else moved.

## Limitations

Stated plainly, because they bound what the output supports:

- **NSE main-board series-EQ only.** BSE-only listings are outside the frame. The
  manifest records `universe_claim: partial`.
- **Fundamentals come from an aggregator**, not from filings.
- **No normalisation of exceptional items.** Reported EPS and PAT only; names
  where other income exceeds 35% of PBT carry an explicit flag.
- **Net debt is gross borrowings.** Cash is not separately available, so leverage
  is overstated for cash-rich companies.
- **Promoter pledging, auditor qualifications, related-party transactions and
  CWIP ageing are not assessed.** All need the annual report.
- **Capex evidence is balance-sheet inference**, not verified commissioning.
- **Event flags are keyword-classified**, not read in full.
- **`available_at` is scrape time, not publication time.** Conservative, but the
  screen cannot be honestly backtested before the first scrape date.
- **No forward estimates.** Every growth figure is realised, not forecast.

## Licence and use

The scrapers here are paced and polite, but you are responsible for complying
with each source's terms of service. screener.in in particular is a third-party
aggregator; check its terms before running the fundamentals sync at volume.

Nothing in this repository is investment advice.
