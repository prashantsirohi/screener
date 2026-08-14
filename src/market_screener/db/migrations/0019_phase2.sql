-- Phase 2: forensic and valuation validation of the Phase 1 candidate set.
--
-- Phase 1 deliberately refuses to judge price - "Undervalued" is a banned tag
-- there, and QC03 enforces it - and it applies no forensic tests beyond a
-- single earnings-quality flag. Phase 2 is where both happen, narrowing ~150
-- names to 40-60.
--
-- What this is NOT: the document-based review the original brief describes.
-- Auditor qualifications, related-party detail, promoter pledging and CWIP
-- ageing all need the annual report, and no filing has ever been fetched
-- (0 of 246 discovered documents, across 5 of 2,086 companies). Every check
-- here is computed from the statement history already in the store, and the
-- questions that need a filing are carried forward as explicit open items
-- rather than quietly dropped.

-- One row per company reviewed.
CREATE TABLE market.phase2_assessment (
    run_id                  text        NOT NULL REFERENCES market.screen_run(run_id) ON DELETE CASCADE,
    security_id             bigint      NOT NULL REFERENCES market.security(security_id) ON DELETE CASCADE,
    phase1_run_id           text        NOT NULL,
    phase1_rank             integer,
    symbol                  text        NOT NULL,
    company                 text,
    primary_archetype       text,

    -- Forensic side. `severity` is the worst flag raised; `forensic_score` is
    -- 0-100 where 100 is clean, so it sorts the same way as everything else.
    forensic_score          numeric(6, 2),
    forensic_severity       text,        -- clean | watch | concern | disqualifying
    flags_raised            integer     NOT NULL DEFAULT 0,

    -- Valuation side. Deliberately several methods and no single fair value:
    -- one number would imply a precision this data cannot support.
    valuation_score         numeric(6, 2),
    valuation_verdict       text,        -- cheap | fair | full | stretched | unassessable
    pe_percentile_5y        numeric(6, 2),
    implied_growth_pct      numeric(10, 4),

    combined_score          numeric(6, 2),
    verdict                 text        NOT NULL,   -- advance | hold | reject
    verdict_reason          text,
    open_questions          text,        -- what still needs a filing to answer
    rank                    integer,
    assessed_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, security_id)
);

CREATE INDEX phase2_assessment_verdict_idx
    ON market.phase2_assessment (run_id, verdict, rank);

-- Long form so every flag carries its own evidence and can be audited or
-- counted independently. A single packed text column would make "how many
-- companies failed on cash conversion" unanswerable.
CREATE TABLE market.phase2_flag (
    run_id          text        NOT NULL,
    security_id     bigint      NOT NULL,
    flag_code       text        NOT NULL,
    severity        text        NOT NULL,   -- watch | concern | disqualifying
    value_num       numeric(20, 6),
    evidence        text        NOT NULL,
    PRIMARY KEY (run_id, security_id, flag_code),
    FOREIGN KEY (run_id, security_id)
        REFERENCES market.phase2_assessment(run_id, security_id) ON DELETE CASCADE
);

CREATE INDEX phase2_flag_code_idx ON market.phase2_flag (run_id, flag_code);

-- Every valuation method's answer, retained separately. When two methods
-- disagree that disagreement IS the finding, and storing only a blended score
-- would hide it.
CREATE TABLE market.phase2_valuation (
    run_id          text        NOT NULL,
    security_id     bigint      NOT NULL,
    method          text        NOT NULL,
    value_num       numeric(20, 6),
    verdict         text,
    basis           text        NOT NULL,   -- what the number was computed from
    PRIMARY KEY (run_id, security_id, method),
    FOREIGN KEY (run_id, security_id)
        REFERENCES market.phase2_assessment(run_id, security_id) ON DELETE CASCADE
);
