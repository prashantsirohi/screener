-- Prices, corporate actions, adjustment, weekly bars.
--
-- price_daily holds RAW bhavcopy values and is append-only: what the exchange
-- published on the day never changes. Adjustment is derived into a separate
-- table so the raw record is never rewritten when a split is discovered later.

CREATE TABLE market.price_daily (
    security_id   bigint       NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    trade_date    date         NOT NULL,
    open          numeric(18, 4),
    high          numeric(18, 4),
    low           numeric(18, 4),
    close         numeric(18, 4),
    prev_close    numeric(18, 4),
    volume        bigint,
    -- Turnover is the whole reason bhavcopy is the source of record: it is what
    -- the liquidity gate needs and Yahoo does not provide.
    turnover_inr  numeric(22, 2),
    trade_count   bigint,
    series        text,
    source        text         NOT NULL DEFAULT 'nse_bhavcopy',
    ingested_at   timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, trade_date)
);

CREATE INDEX price_daily_date_idx ON market.price_daily (trade_date);
CREATE INDEX price_daily_security_date_idx
    ON market.price_daily (security_id, trade_date DESC);

COMMENT ON TABLE market.price_daily IS
    'Append-only raw daily bars from NSE bhavcopy. Never updated in place.';


-- Corporate actions. Sourced from the NSE API and cross-checked against a
-- prev-close discontinuity inference, since the API is not exhaustive.
CREATE TABLE market.corporate_action (
    corporate_action_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    security_id      bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    ex_date          date        NOT NULL,
    action_type      text        NOT NULL,   -- split | bonus | dividend | rights | demerger
    ratio_from       numeric(18, 6),
    ratio_to         numeric(18, 6),
    amount_inr       numeric(18, 4),
    -- Multiply pre-ex-date prices by this to put them on the post-ex basis.
    adjustment_factor numeric(18, 10),
    purpose_text     text,
    source           text        NOT NULL,   -- nse_api | inferred_prev_close | manual
    confidence       text        NOT NULL DEFAULT 'reported',  -- reported | inferred | conflicted
    action_hash      text        NOT NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT corporate_action_hash_key UNIQUE (action_hash)
);

CREATE INDEX corporate_action_security_idx
    ON market.corporate_action (security_id, ex_date DESC);

COMMENT ON COLUMN market.corporate_action.confidence IS
    'inferred = derived from a prev_close discontinuity with no matching API '
    'record; conflicted = API and inference disagree and a human should look.';


-- Derived adjusted series. Rebuildable from price_daily + corporate_action, so
-- it is safe to truncate and recompute.
CREATE TABLE market.price_daily_adj (
    security_id       bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    trade_date        date        NOT NULL,
    adj_open          numeric(18, 6),
    adj_high          numeric(18, 6),
    adj_low           numeric(18, 6),
    adj_close         numeric(18, 6),
    adj_volume        bigint,
    cum_adj_factor    numeric(18, 10) NOT NULL DEFAULT 1.0,
    adj_basis         text        NOT NULL DEFAULT 'split_bonus',
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, trade_date)
);

COMMENT ON COLUMN market.price_daily_adj.adj_basis IS
    'split_bonus = price return (bhavcopy + corporate actions). '
    'yahoo_adjclose = total return, which also removes dividends.';


-- Weekly bars, W-FRI. The Weinstein layer reads only this table.
--
-- week_end_date is ALWAYS the Friday of the ISO week. Yahoo timestamps are
-- week-start (Monday) and bhavcopy resamples to week-end; normalising both to
-- the same Friday at load is what stops the stock/benchmark join from silently
-- dropping every row and returning NULL relative strength.
CREATE TABLE market.weekly_bar (
    security_id     bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    week_end_date   date        NOT NULL,
    iso_year        smallint    NOT NULL,
    iso_week        smallint    NOT NULL,
    open            numeric(18, 6),
    high            numeric(18, 6),
    low             numeric(18, 6),
    close           numeric(18, 6),
    volume          bigint,
    turnover_inr    numeric(22, 2),
    day_count       smallint,
    source          text        NOT NULL,   -- nse_bhavcopy | yahoo_weekly
    -- Higher rank wins when two sources cover the same week, so the bhavcopy
    -- backfill displaces Yahoo per security with no cutover event.
    source_rank     smallint    NOT NULL DEFAULT 50,
    adj_basis       text        NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, week_end_date)
);

CREATE INDEX weekly_bar_week_idx ON market.weekly_bar (week_end_date);
CREATE INDEX weekly_bar_security_week_idx
    ON market.weekly_bar (security_id, week_end_date DESC);

ALTER TABLE market.weekly_bar
    ADD CONSTRAINT weekly_bar_iso_week_ck CHECK (iso_week BETWEEN 1 AND 53);


-- Where bhavcopy-derived and Yahoo-derived weekly closes disagree. Populated at
-- cutover; a security that never reaches 'agree' should not silently flip.
CREATE TABLE market.price_source_reconciliation (
    security_id      bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    as_of_date       date        NOT NULL,
    weeks_compared   integer     NOT NULL,
    weeks_matching   integer     NOT NULL,
    max_abs_diff_pct numeric(12, 6),
    median_diff_pct  numeric(12, 6),
    verdict          text        NOT NULL,   -- agree | drift | disagree | insufficient
    note             text,
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, as_of_date)
);
