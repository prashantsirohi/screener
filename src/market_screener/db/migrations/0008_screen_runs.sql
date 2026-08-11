-- Run tracking and artifacts.
--
-- Every screen run is retained so two runs can be diffed. This is the thing the
-- JSON-cache pipeline could not do: it overwrote its own output, so "what
-- changed since last week and why" was unanswerable.

CREATE TABLE market.screen_run (
    run_id                text        PRIMARY KEY,
    phase                 smallint    NOT NULL,
    status                text        NOT NULL DEFAULT 'running',  -- running | complete | partial | failed
    started_at            timestamptz NOT NULL DEFAULT now(),
    completed_at          timestamptz,
    -- The single as_of threaded through every stage. No stage may call now().
    as_of_date            date        NOT NULL,
    price_date            date,
    technical_data_date   date,
    financial_data_cutoff date,
    universe_claim        text        NOT NULL DEFAULT 'partial',
    universe_description  text,
    config_hash           text,
    params                jsonb,
    counts                jsonb,
    known_limitations     jsonb,
    note                  text
);

CREATE INDEX screen_run_started_idx ON market.screen_run (started_at DESC);

COMMENT ON COLUMN market.screen_run.as_of_date IS
    'Bound as a parameter into every analytics query. Rule 3 of the two-engine '
    'contract: no now()/current_date anywhere in analytics SQL.';


CREATE TABLE market.screen_stage (
    run_id        text        NOT NULL REFERENCES market.screen_run(run_id) ON DELETE CASCADE,
    stage         text        NOT NULL,
    attempt       smallint    NOT NULL DEFAULT 1,
    status        text        NOT NULL DEFAULT 'running',  -- running | complete | skipped | failed
    skip_reason   text,
    input_hash    text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    heartbeat_at  timestamptz,
    rows_in       bigint,
    rows_out      bigint,
    error         text,
    PRIMARY KEY (run_id, stage, attempt)
);

COMMENT ON COLUMN market.screen_stage.input_hash IS
    'Hash of the stage inputs. An identical hash on a later run means the stage '
    'can be skipped as unchanged_stage_inputs.';


CREATE TABLE market.screen_artifact (
    run_id        text        NOT NULL REFERENCES market.screen_run(run_id) ON DELETE CASCADE,
    stage         text        NOT NULL,
    attempt       smallint    NOT NULL DEFAULT 1,
    artifact_name text        NOT NULL,
    artifact_type text        NOT NULL,   -- csv | md | json | parquet
    path          text        NOT NULL,
    row_count     bigint,
    sha256        text        NOT NULL,
    bytes         bigint,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, stage, attempt, artifact_name)
);


CREATE TABLE market.screen_qc_result (
    run_id      text        NOT NULL REFERENCES market.screen_run(run_id) ON DELETE CASCADE,
    check_id    text        NOT NULL,
    check_name  text        NOT NULL,
    passed      boolean     NOT NULL,
    detail      text,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, check_id)
);


-- Source log, generated from real ingest provenance rather than hardcoded.
CREATE TABLE market.screen_source_log (
    run_id                    text NOT NULL REFERENCES market.screen_run(run_id) ON DELETE CASCADE,
    source_id                 text NOT NULL,
    security_id               bigint REFERENCES market.security(security_id) ON DELETE SET NULL,
    company                   text,
    symbol                    text,
    document_type             text,
    title                     text,
    issuer                    text,
    published_date            date,
    period_covered            text,
    url                       text,
    primary_or_secondary      text NOT NULL,
    accessed_date             date,
    claim_or_metric_supported text,
    notes                     text,
    PRIMARY KEY (run_id, source_id)
);
