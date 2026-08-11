-- Phase 1 outputs, one row set per run.
--
-- Column names mirror the frozen 37-column P1_screened_universe.csv contract so
-- the CSV is a straight projection of this table, not a separate construction.
-- Append-only per run_id; runs are never overwritten, which is what makes
-- `screener runs diff` possible.

CREATE TABLE market.phase1_universe (
    run_id                      text        NOT NULL REFERENCES market.screen_run(run_id) ON DELETE CASCADE,
    security_id                 bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    screening_date              date        NOT NULL,
    company                     text,
    symbol                      text        NOT NULL,
    exchange                    text,
    listing_board               text,
    sector                      text,
    industry                    text,
    current_price_inr           numeric(18, 4),
    price_date                  date,
    market_cap_inr_cr           numeric(20, 4),
    market_cap_date             date,
    -- Not in the CSV contract, but records HOW the market cap was obtained
    -- (aggregator / issued-size / computed) so the gate is auditable.
    market_cap_method           text,
    liquidity_metric_name       text,
    liquidity_value_inr_cr      numeric(18, 4),
    liquidity_period            text,
    eligible_flag               smallint    NOT NULL,
    exclusion_code              text,
    exclusion_reason            text,
    primary_archetype           text,
    classification_rationale    text,
    secondary_tags              text,
    technical_stage             text,
    technical_data_date         date,
    revenue_cagr_5y_pct         numeric(14, 6),
    normalized_eps_cagr_5y_pct  numeric(14, 6),
    latest_roce_or_roe_pct      numeric(14, 6),
    median_roce_or_roe_5y_pct   numeric(14, 6),
    net_debt_to_equity          numeric(14, 6),
    cfo_pat_period              text,
    cfo_pat_ratio               numeric(14, 6),
    preliminary_valuation_method text,
    preliminary_valuation_value numeric(14, 4),
    preliminary_priority_score  numeric(8, 2),
    inclusion_reason            text,
    key_disqualifying_risk      text,
    data_quality_confidence     text,
    primary_source_ids          text,
    secondary_source_ids        text,
    PRIMARY KEY (run_id, security_id)
);

CREATE INDEX phase1_universe_eligible_idx
    ON market.phase1_universe (run_id, eligible_flag, preliminary_priority_score DESC);
CREATE INDEX phase1_universe_symbol_idx ON market.phase1_universe (symbol);
CREATE INDEX phase1_universe_archetype_idx
    ON market.phase1_universe (run_id, primary_archetype) WHERE eligible_flag = 1;


-- Selected candidates: the extra four columns of the P1_candidates.csv contract.
CREATE TABLE market.phase1_candidate (
    run_id                     text     NOT NULL,
    security_id                bigint   NOT NULL,
    rank                       integer  NOT NULL,
    phase2_priority            text     NOT NULL,   -- High | Medium | Low
    phase2_questions           text,
    required_primary_documents text,
    known_data_gaps            text,
    PRIMARY KEY (run_id, security_id),
    FOREIGN KEY (run_id, security_id)
        REFERENCES market.phase1_universe(run_id, security_id) ON DELETE CASCADE
);

CREATE INDEX phase1_candidate_rank_idx ON market.phase1_candidate (run_id, rank);


-- The six weighted components behind preliminary_priority_score, kept separate
-- so a score can be explained rather than just asserted.
CREATE TABLE market.phase1_score_component (
    run_id       text          NOT NULL,
    security_id  bigint        NOT NULL,
    component    text          NOT NULL,
    weight       numeric(6, 2) NOT NULL,
    value        numeric(8, 2) NOT NULL,
    PRIMARY KEY (run_id, security_id, component),
    FOREIGN KEY (run_id, security_id)
        REFERENCES market.phase1_universe(run_id, security_id) ON DELETE CASCADE
);


-- Archetype fit scores for every archetype, not just the winner. Makes the
-- argmax decision reviewable and shows how close the runner-up was.
CREATE TABLE market.phase1_archetype_fit (
    run_id      text          NOT NULL,
    security_id bigint        NOT NULL,
    archetype   text          NOT NULL,
    fit_score   numeric(8, 2) NOT NULL,
    is_primary  boolean       NOT NULL DEFAULT false,
    PRIMARY KEY (run_id, security_id, archetype),
    FOREIGN KEY (run_id, security_id)
        REFERENCES market.phase1_universe(run_id, security_id) ON DELETE CASCADE
);
