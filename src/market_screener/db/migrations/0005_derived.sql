-- Derived metrics and features, long form and run-stamped.
--
-- Long form because the metric set changes as the screen evolves and a wide
-- table would need a migration per new field. Run-stamped so a run's inputs can
-- be reconstructed exactly as they were, even after a later sync.

CREATE TABLE market.company_metric (
    security_id  bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    as_of_date   date        NOT NULL,
    metric       text        NOT NULL,
    value_num    numeric(24, 6),
    value_text   text,
    computed_by  text        NOT NULL,   -- module that produced it
    run_id       text,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, as_of_date, metric)
);

CREATE INDEX company_metric_metric_idx ON market.company_metric (metric, as_of_date DESC);
CREATE INDEX company_metric_run_idx ON market.company_metric (run_id) WHERE run_id IS NOT NULL;


-- Technical features, one row per security per week.
--
-- Every column here is computed in DuckDB SQL. Python must not recompute any of
-- them: that is rule 2 of the two-engine contract. weinstein.analyse() consumes
-- this row and contributes only find_base() and the stage decision tree.
CREATE TABLE market.technical_feature (
    security_id             bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    week_end_date           date        NOT NULL,
    close                   numeric(18, 6),
    ma30w                   numeric(18, 6),
    ma40w                   numeric(18, 6),
    ma30w_slope_pct         numeric(12, 4),
    ma40w_slope_pct         numeric(12, 4),
    dist_from_ma30w_pct     numeric(12, 4),
    high_52w                numeric(18, 6),
    low_52w                 numeric(18, 6),
    dist_from_52w_high_pct  numeric(12, 4),
    return_13w_pct          numeric(12, 4),
    return_26w_pct          numeric(12, 4),
    return_52w_pct          numeric(12, 4),
    rs_bm_13w_pct           numeric(12, 4),
    rs_bm_52w_pct           numeric(12, 4),
    rs_sector_13w_pct       numeric(12, 4),
    rs_sector_52w_pct       numeric(12, 4),
    vol_4w_avg              numeric(22, 2),
    vol_20w_avg             numeric(22, 2),
    vol_50w_avg             numeric(22, 2),
    vol_ratio_4w_20w        numeric(12, 4),
    weeks_above_ma30        integer,
    overhead_supply_pct     numeric(12, 4),
    liquidity_inr_cr        numeric(18, 4),
    weeks_history           integer,
    benchmark_symbol        text,
    sector_benchmark        text,
    adj_basis               text,
    bar_source              text,
    computed_at             timestamptz NOT NULL DEFAULT now(),
    run_id                  text,
    PRIMARY KEY (security_id, week_end_date)
);

CREATE INDEX technical_feature_week_idx ON market.technical_feature (week_end_date);


-- Event-derived features per security, decayed and materiality-weighted.
CREATE TABLE market.event_feature (
    security_id    bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    as_of_date     date        NOT NULL,
    event_class    text        NOT NULL,
    tier           text,
    latest_event_at timestamptz,
    event_count    integer     NOT NULL DEFAULT 0,
    decayed_weight numeric(12, 6),
    materiality    numeric(12, 6),
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (security_id, as_of_date, event_class)
);
