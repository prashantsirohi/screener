-- Sync infrastructure: watermarks, batches, errors, and the retry queue.
--
-- The retry queue lives here rather than in memory or a file because the whole
-- point is that it survives a crash, a reboot, or a killed run. market_intel
-- kept its scheduler cursors in process memory and lost them on restart.

CREATE TABLE market.sync_watermark (
    source        text        NOT NULL,
    scope         text        NOT NULL DEFAULT '*',  -- '*' or a symbol/index name
    watermark     text,                              -- ISO date or timestamp, source-defined
    last_run_at   timestamptz,
    last_status   text,
    rows_written  bigint      NOT NULL DEFAULT 0,
    note          text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, scope)
);

COMMENT ON TABLE market.sync_watermark IS
    'High-water mark per source. Upserted. Absence means "never synced", which '
    'a collector must treat as a full backfill rather than an empty window.';


CREATE TABLE market.sync_batch (
    sync_batch_id     text        PRIMARY KEY,
    source            text        NOT NULL,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    status            text        NOT NULL DEFAULT 'running',  -- running | complete | partial | failed
    items_total       integer     NOT NULL DEFAULT 0,
    items_succeeded   integer     NOT NULL DEFAULT 0,
    items_failed      integer     NOT NULL DEFAULT 0,
    items_skipped     integer     NOT NULL DEFAULT 0,
    rows_written      bigint      NOT NULL DEFAULT 0,
    params            jsonb,
    note              text
);

CREATE INDEX sync_batch_source_idx ON market.sync_batch (source, started_at DESC);


CREATE TABLE market.sync_error (
    sync_error_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sync_batch_id text        REFERENCES market.sync_batch(sync_batch_id) ON DELETE CASCADE,
    source        text        NOT NULL,
    scope         text,                        -- symbol or window that failed
    error_class   text,
    error         text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX sync_error_batch_idx ON market.sync_error (sync_batch_id);
CREATE INDEX sync_error_scope_idx ON market.sync_error (source, scope);


-- Persisted, escalating retry queue. Primarily for screener.in blank pages, but
-- generic enough for any per-item fetch failure.
CREATE TABLE market.fetch_retry_queue (
    retry_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          text        NOT NULL,
    scope           text        NOT NULL,      -- usually the symbol
    reason          text        NOT NULL,      -- blank_page | http_error | parse_error
    attempts        integer     NOT NULL DEFAULT 0,
    state           text        NOT NULL DEFAULT 'pending',  -- pending | in_flight | resolved | exhausted
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    claimed_at      timestamptz,
    last_error      text,
    first_queued_at timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fetch_retry_queue_key UNIQUE (source, scope)
);

-- The claim query is `state='pending' AND next_attempt_at <= now()` ordered by
-- next_attempt_at; this index serves it directly.
CREATE INDEX fetch_retry_queue_due_idx
    ON market.fetch_retry_queue (state, next_attempt_at)
    WHERE state = 'pending';

COMMENT ON TABLE market.fetch_retry_queue IS
    'Rows are claimed with UPDATE ... SET state=''in_flight'' ... RETURNING so '
    'two concurrent runs cannot fetch the same item.';
