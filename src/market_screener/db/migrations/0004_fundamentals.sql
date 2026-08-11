-- Point-in-time fundamentals.
--
-- Long/EAV form with available_at IN THE PRIMARY KEY. That single decision is
-- what makes the store point-in-time: a restatement of FY2025 scraped in 2027
-- coexists with the figure as it was known in 2026, so an as-of query can never
-- see a number that had not been published yet.
--
-- Caveat carried forward: available_at is currently SCRAPE time, not publication
-- time. That is conservative (never optimistic) but means the screen cannot be
-- honestly backtested before the first scrape date.

CREATE TABLE market.metric_dim (
    metric_id        text PRIMARY KEY,
    metric_label     text NOT NULL,          -- the label as it appears at source
    statement        text NOT NULL,          -- profit_loss | balance_sheet | cash_flow | ratios | quarters | shareholding | top_ratio
    unit             text NOT NULL,          -- inr_cr | inr | pct | ratio | days | count
    higher_is_better boolean,
    description      text
);

COMMENT ON TABLE market.metric_dim IS
    'Controlled vocabulary for fundamental metrics. Scraped row labels are '
    'mapped here so a source-side wording change is a data fix, not a schema change.';


-- Raw page retained. Keeping the payload means a parser bug can be fixed and
-- replayed without re-hitting the source, and blank pages are evidence rather
-- than an absence.
CREATE TABLE market.screener_page_raw (
    page_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id   bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    basis         text        NOT NULL,      -- consolidated | standalone
    source_url    text        NOT NULL,
    fetched_at    timestamptz NOT NULL,
    payload       jsonb       NOT NULL,
    payload_hash  text        NOT NULL,
    -- True when the page rendered as a template shell: row labels present but
    -- every numeric span empty. Detected structurally, not by HTTP status.
    is_blank      boolean     NOT NULL DEFAULT false,
    blank_reason  text,
    parser_version text       NOT NULL DEFAULT 'v1',
    CONSTRAINT screener_page_raw_dedup UNIQUE (security_id, basis, fetched_at)
);

CREATE INDEX screener_page_raw_security_idx
    ON market.screener_page_raw (security_id, fetched_at DESC);
CREATE INDEX screener_page_raw_blank_idx
    ON market.screener_page_raw (is_blank) WHERE is_blank;


CREATE TABLE market.screener_fact (
    security_id      bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    period_type      text        NOT NULL,   -- annual | quarter | ttm
    report_date      date        NOT NULL,
    statement_basis  text        NOT NULL,   -- consolidated | standalone
    metric_id        text        NOT NULL REFERENCES market.metric_dim(metric_id),
    value            numeric(24, 6),
    available_at     timestamptz NOT NULL,
    source           text        NOT NULL DEFAULT 'screener',
    page_id          bigint      REFERENCES market.screener_page_raw(page_id) ON DELETE SET NULL,
    sync_batch_id    text,
    PRIMARY KEY (security_id, period_type, report_date, statement_basis,
                 metric_id, available_at)
);

CREATE INDEX screener_fact_lookup_idx
    ON market.screener_fact (security_id, metric_id, period_type, report_date DESC);
CREATE INDEX screener_fact_available_idx
    ON market.screener_fact (available_at);

COMMENT ON COLUMN market.screener_fact.available_at IS
    'Knowledge date. Part of the PK so restatements never overwrite history. '
    'An as-of query must filter available_at <= as_of.';


-- Latest known value per (security, period, metric), the view the screen reads.
CREATE VIEW market.screener_fact_current AS
SELECT DISTINCT ON (security_id, period_type, report_date, statement_basis, metric_id)
       security_id, period_type, report_date, statement_basis, metric_id,
       value, available_at, source, page_id
FROM   market.screener_fact
ORDER  BY security_id, period_type, report_date, statement_basis, metric_id,
          available_at DESC;

COMMENT ON VIEW market.screener_fact_current IS
    'Most recently known value per fact. For a historical run, query '
    'screener_fact directly with an available_at <= as_of filter instead.';
