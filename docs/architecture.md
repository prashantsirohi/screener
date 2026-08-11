# Architecture

## Layout

```
src/market_screener/
  config.py            Settings: env > .env > code defaults; config_hash
  paths.py             DataDomain (operational|research), overridable roots
  cli.py               command surface
  db/
    connection.py      psycopg access; the ONLY writer
    migrate.py         numbered .sql + schema_version ledger + advisory lock
    copy_io.py         COPY into staging, then one INSERT ... ON CONFLICT
    migrations/        0001..0015_*.sql
  http/
    client.py          warmup, pacing, two-level retry, bot-wall detection
    errors.py          Temporary / Permanent / BlankPage taxonomy
  sources/             one module per external source
  ingest/              sync orchestration, watermarks, retry queue
  analytics/
    duck.py            DuckDB session; src_* views; attach|parquet modes
    sql/               scan and reshape only - no formulas
    features.py        loads bars, calls the domain layer
  domain/              the actual screening logic
  pipeline/
    context.py         RunContext / StageResult / StageArtifact
    orchestrator.py    PIPELINE_ORDER, resume, input-hash skip
    stages/            s80 screen, s85 outputs, s90 summary, s95 QC
tests/
  unit/ integration/ parity/ reference/
legacy_scripts/        the original 26 scripts, frozen, never imported
phase1_baseline_.../   frozen pre-rewrite output - the parity oracle
```

## Storage: Postgres writes, DuckDB reads

Postgres is the source of record. Every write goes through psycopg with bound
parameters, identity columns and real foreign keys.

DuckDB attaches **read-only** (`ATTACH … TYPE postgres, READ_ONLY`) for bulk
scans and joins. Results return as Arrow and are written back by psycopg. DuckDB
*can* write to Postgres; it is forbidden here, because two writers means two
owners of the same rows.

A Parquet fallback exists for hosts that cannot download the DuckDB `postgres`
extension. Both modes expose the same `src_*` views, so switching is a config
change rather than a rewrite.

### The two-engine contract

1. **DuckDB never writes to Postgres.** One writer, one transaction boundary.
2. **A formula lives in exactly one engine.** DuckDB does the scan and the
   benchmark join; the Weinstein arithmetic is Python and only Python. This is
   why technical parity came out exact rather than approximately right.
3. **One `as_of`, threaded everywhere.** No `now()` or `current_date` in any
   analytics SQL — a run must replay to the same answer.
4. **Cast to `DOUBLE` once**, immediately after the source view, so pandas and
   DuckDB agree on float64.
5. **A permanent parity test** against a frozen copy of the pre-rewrite code.
   It is never deleted.

## Schema

One `market` schema, 15 migrations. Grouped by concern:

### Security master

`security` (surrogate id, ISIN as the natural key, `is_active`),
`security_alias` (symbol history), `index_membership`, `trading_calendar`.

ISIN is the natural key because it survives a symbol change. `security_alias`
retains every ticker a security has traded under, which is what lets a
three-year backfill attach an old bhavcopy row to today's company.

`is_active` means *currently in `EQUITY_L`*. The backfill registers securities
that have since delisted or merged so their history has somewhere to live; they
are inactive and are never screened.

### Prices

`price_daily` — **append-only**, raw bhavcopy. What the exchange published on
the day does not change, so it is never rewritten. Includes `turnover_inr`,
which is the whole reason bhavcopy is the source of record.

`corporate_action` — hash-deduped, with a `confidence` of `reported` (the NSE
feed), `corroborated` (two independent sources agree), `inferred` (a single
unambiguous price gap) or `unconfirmed` (a shallow gap that could equally be a
bad day — recorded, but **excluded from adjustment**).

`price_daily_adj` — derived, rebuildable. Prices multiplied by the cumulative
factor of every action taking effect after that bar.

`weekly_bar` — PK `(security_id, week_end_date, source)`. Both sources are
retained so they can be reconciled against each other;
`weekly_bar_resolved` elects one per security.

`price_source_reconciliation` — per-security verdict comparing the two series.

### Point-in-time fundamentals

`screener_fact` in long/EAV form, with **`available_at` in the primary key**:

```
PRIMARY KEY (security_id, period_type, report_date, statement_basis,
             metric_id, available_at)
```

That one decision is what makes the store point-in-time. A restatement scraped
later coexists with the figure as it was known before, and an as-of query filters
`available_at <= as_of`.

`screener_page_raw` retains the full payload and an `is_blank` flag.
`metric_dim` is the controlled vocabulary that scraped row labels map onto.

### Events

`announcement` (PK on a content hash, with `available_at` and a genuinely
persisted `seen_count`), `announcement_classification` (versioned, so two
taxonomies coexist), `document`.

### Sync infrastructure

`sync_watermark`, `sync_batch`, `sync_error`, `fetch_retry_queue`.

The retry queue lives in Postgres precisely so a crash cannot lose work. Rows are
claimed with `UPDATE … RETURNING` inside a committed transaction, and an
abandoned claim is returned to `pending` by a lease timeout.

### Run-versioned outputs

`screen_run`, `screen_stage`, `screen_artifact`, `screen_qc_result`,
`screen_source_log`, `phase1_universe`, `phase1_candidate`,
`phase1_score_component`, `phase1_archetype_fit`.

Every run is retained in full. That is what makes two runs comparable — the thing
the original JSON-cache pipeline could not do, because it overwrote its own
output.

### Write discipline

| Pattern | Tables |
|---|---|
| Append-only, `ON CONFLICT DO NOTHING` | `price_daily`, `screener_fact`, `announcement`, all `phase1_*` |
| Upserted, `ON CONFLICT DO UPDATE` | `security`, `sync_watermark`, `fetch_retry_queue` |
| Derived, safe to truncate and rebuild | `price_daily_adj`, `weekly_bar`, `technical_feature` |

Bulk loads go `COPY` → `staging.*` → one `INSERT … SELECT … ON CONFLICT`, so the
conflict policy is visible in SQL and the load is one transaction.

## Migrations

Numbered `.sql` files plus a `schema_version` ledger recording each file's
SHA-256. A migration edited after being applied halts the runner rather than
layering more changes onto an unknown base. An advisory lock stops two `migrate`
runs interleaving. No Alembic.

## Pipeline

`PIPELINE_ORDER` is sequential:

```
s80_phase1_screen  → eligibility, classification, scoring; writes phase1_* rows
s85_phase1_outputs → candidate selection, provenance source log, CSVs
s90_summary        → P1_summary.md and P1_run_manifest.json
s95_qc             → 18 checks, persisted to screen_qc_result
```

Each stage records its input hash. If the hash matches a previous completed run
for the same `as_of`, the stage is skipped — but a skipped stage still carries its
prior output forward into the new run, because downstream stages read by `run_id`
and every run must be self-contained for `runs diff` to mean anything.

`--resume` continues the most recent unfinished run at the right stage.

## Domain layer

`metrics.py` and `archetypes.py` are byte-identical copies of the pre-rewrite
code; `scoring.py` was extracted verbatim. They are covered by parity tests, so
the risk was never the arithmetic — it was the data path, which is exactly what
those tests exercise.

`weinstein.py` is the same code with its JSON loader replaced by a frame builder;
everything below that line is untouched.

`eligibility.py` holds the gates, applied in order and short-circuiting — the
first gate a company fails is its recorded exclusion code, so reordering them
would relabel exclusions even if the eligible set were unchanged.
