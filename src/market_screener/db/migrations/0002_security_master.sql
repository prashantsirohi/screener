-- Security master.
--
-- Keyed on a surrogate id with ISIN as the natural key where available. ISIN is
-- stable across NSE symbol changes, which is what makes price and fundamental
-- history survive a rename. The symbol lives in security_alias so a history of
-- renames is retained rather than overwritten.

CREATE TABLE market.security (
    security_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    isin            text,
    symbol          text        NOT NULL,
    exchange        text        NOT NULL DEFAULT 'NSE',
    series          text,
    security_type   text        NOT NULL DEFAULT 'equity',   -- equity | index
    company_name    text,
    face_value      numeric(18, 4),
    listing_date    date,
    nse_industry    text,
    is_active       boolean     NOT NULL DEFAULT true,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT security_symbol_exchange_key UNIQUE (symbol, exchange)
);

-- ISIN is unique when present; indices and a few instruments have none.
CREATE UNIQUE INDEX security_isin_key
    ON market.security (isin) WHERE isin IS NOT NULL;
CREATE INDEX security_active_idx
    ON market.security (is_active, security_type);
CREATE INDEX security_industry_idx
    ON market.security (nse_industry) WHERE nse_industry IS NOT NULL;

COMMENT ON COLUMN market.security.isin IS
    'Natural key. Stable across symbol changes; NULL for indices.';
COMMENT ON COLUMN market.security.security_type IS
    'equity = tradable scrip; index = benchmark series used for relative strength.';


-- Symbol history. A row per (symbol, exchange) ever observed for a security.
CREATE TABLE market.security_alias (
    security_id   bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    symbol        text        NOT NULL,
    exchange      text        NOT NULL DEFAULT 'NSE',
    valid_from    date        NOT NULL DEFAULT CURRENT_DATE,
    valid_to      date,
    source        text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, symbol, exchange, valid_from)
);

CREATE INDEX security_alias_symbol_idx ON market.security_alias (symbol, exchange);


-- Index constituency, snapshotted. Supplies NSE's own industry classification
-- and lets the screen ask "was this in the Nifty 500 on date X".
CREATE TABLE market.index_membership (
    snapshot_date date        NOT NULL,
    index_name    text        NOT NULL,
    security_id   bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    industry      text,
    source        text        NOT NULL,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_date, index_name, security_id)
);

CREATE INDEX index_membership_security_idx
    ON market.index_membership (security_id, snapshot_date DESC);


-- Trading calendar, derived from observed bhavcopy dates plus explicit holidays.
-- The incremental price sync walks this backwards to find the last session.
CREATE TABLE market.trading_calendar (
    exchange     text        NOT NULL DEFAULT 'NSE',
    trade_date   date        NOT NULL,
    is_trading   boolean     NOT NULL,
    source       text        NOT NULL,
    note         text,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (exchange, trade_date)
);

CREATE INDEX trading_calendar_trading_idx
    ON market.trading_calendar (exchange, trade_date DESC) WHERE is_trading;
