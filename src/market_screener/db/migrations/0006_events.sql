-- Corporate announcements, classification, and filing documents.
--
-- Announcements are append-only and deduped on a content hash, following the
-- market_intel pattern. Unlike market_intel, seen_count is genuinely persisted
-- and available_at is a real column, because event-driven screening needs to
-- know when a disclosure became knowable, not when it was dated.

CREATE TABLE market.announcement (
    announcement_hash text        PRIMARY KEY,
    security_id       bigint      REFERENCES market.security(security_id) ON DELETE SET NULL,
    raw_symbol        text,
    raw_isin          text,
    source            text        NOT NULL,   -- nse_announcements | bse_corp
    external_id       text,
    subject           text,
    description       text,
    announced_at      timestamptz,
    -- Knowledge date: when this became visible to us. Event queries filter on
    -- this, not on announced_at, so a backfilled window cannot leak lookahead.
    available_at      timestamptz NOT NULL,
    attachment_url    text,
    seen_count        integer     NOT NULL DEFAULT 1,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    sync_batch_id     text
);

CREATE INDEX announcement_security_time_idx
    ON market.announcement (security_id, announced_at DESC);
CREATE INDEX announcement_available_idx
    ON market.announcement (available_at);
-- BRIN suits an append-mostly, time-ordered table at a fraction of the size of
-- a btree. At 5 years the table is ~900k rows of mostly attachment text; do not
-- add a trigram index here by default.
CREATE INDEX announcement_announced_brin
    ON market.announcement USING brin (announced_at);


CREATE TABLE market.announcement_classification (
    announcement_hash text        NOT NULL
        REFERENCES market.announcement(announcement_hash) ON DELETE CASCADE,
    taxonomy_version  text        NOT NULL,
    primary_category  text        NOT NULL,
    tier              text,                   -- A | B | C | IGNORE
    importance        numeric(6, 2),
    matched_keyword   text,
    classified_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (announcement_hash, taxonomy_version)
);

CREATE INDEX announcement_classification_cat_idx
    ON market.announcement_classification (primary_category, taxonomy_version);

COMMENT ON COLUMN market.announcement_classification.taxonomy_version IS
    'Classification is versioned so the taxonomy can be revised and the old and '
    'new labels compared without re-ingesting announcements.';


-- Primary documents: annual reports, results filings, rating rationales,
-- concall transcripts. Phase 2 reads from here; Phase 1 only populates it.
CREATE TABLE market.document (
    document_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id   bigint      REFERENCES market.security(security_id) ON DELETE CASCADE,
    doc_type      text        NOT NULL,   -- annual_report | results | rating | transcript | presentation | filing
    title         text,
    period        text,
    url           text        NOT NULL,
    issuer        text,
    published_date date,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    local_path    text,
    content_hash  text,
    fetch_status  text        NOT NULL DEFAULT 'not_fetched',
    CONSTRAINT document_url_key UNIQUE (url)
);

CREATE INDEX document_security_type_idx ON market.document (security_id, doc_type);
