-- Make metric-label drift detectable.
--
-- metric_id is minted by slugifying the aggregator's DISPLAY label
-- (metric_map.metric_id). Display labels are not a stable interface - "Sales"
-- is already "Revenue" for lenders - so when a source row is renamed the slug
-- changes and a NEW metric_id quietly appears. The old one simply stops
-- receiving facts.
--
-- Nothing errors. The screen keeps running, the renamed metric reads as absent
-- for every company, and any ratio built on it silently becomes None. That is
-- the same shape as every other defect in this system: a plausible wrong answer,
-- not a failure.
--
-- This table is the observation log that makes the change visible. Each snapshot
-- records what labels existed, what they mapped to, in what units, and how many
-- companies carried them; comparing consecutive snapshots yields the four things
-- worth alerting on - new labels, vanished labels, unit changes, and coverage
-- collapses.

CREATE TABLE market.metric_coverage_snapshot (
    snapshot_at     timestamptz NOT NULL,
    metric_id       text        NOT NULL,
    statement       text        NOT NULL,
    -- The raw display label as observed, kept beside the canonical id it was
    -- mapped to. Storing only the id would lose exactly the evidence needed to
    -- tell "the source renamed this row" from "we changed the mapping".
    metric_label    text        NOT NULL,
    unit            text        NOT NULL,
    -- Fingerprint of the alias/unit/sign tables in metric_map. Distinguishes a
    -- change in the SOURCE from a change in OUR mapping - without it, editing
    -- LABEL_ALIASES looks identical to the aggregator renaming a row.
    mapping_version text        NOT NULL,
    securities      integer     NOT NULL,
    facts           bigint      NOT NULL,
    PRIMARY KEY (snapshot_at, metric_id)
);

CREATE INDEX metric_coverage_snapshot_metric_idx
    ON market.metric_coverage_snapshot (metric_id, snapshot_at DESC);
