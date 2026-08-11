-- One source and one return basis per security, for the whole lookback.
--
-- The previous resolved view picked the highest-ranked source PER WEEK. That
-- silently spliced two different return bases into one series: 1,406 of 2,086
-- active securities ended up with both Yahoo total-return and bhavcopy
-- price-return bars, across 3,024 source transitions.
--
-- This is not cosmetic. Yahoo strips dividends and bhavcopy does not, so at
-- every transition the series steps by the cumulative yield. A 30- or 40-week
-- moving average spanning that seam is averaging two different quantities, and
-- the step reads as price action that never happened - it can create or destroy
-- a stage transition outright.
--
-- The view now chooses ONE source per security: highest rank first, then the
-- longest history, then the most recent. A security's series is homogeneous by
-- construction; there is no per-week splicing left to reason about.
--
-- Basis coherence with the benchmark is a separate requirement and is enforced
-- in the features layer, because relative strength divides a stock series by a
-- benchmark series and the two must be on the same basis to mean anything.

CREATE OR REPLACE VIEW market.weekly_bar_source_choice AS
SELECT DISTINCT ON (security_id)
       security_id,
       source,
       source_rank,
       adj_basis,
       count(*)      OVER (PARTITION BY security_id, source) AS bars,
       min(week_end_date) OVER (PARTITION BY security_id, source) AS first_week,
       max(week_end_date) OVER (PARTITION BY security_id, source) AS last_week
FROM   market.weekly_bar
ORDER  BY security_id,
          source_rank DESC,
          count(*)      OVER (PARTITION BY security_id, source) DESC,
          max(week_end_date) OVER (PARTITION BY security_id, source) DESC,
          source;

COMMENT ON VIEW market.weekly_bar_source_choice IS
    'The single source elected to represent each security. One row per security.';

CREATE OR REPLACE VIEW market.weekly_bar_resolved AS
SELECT w.security_id, w.week_end_date, w.iso_year, w.iso_week,
       w.open, w.high, w.low, w.close, w.volume, w.turnover_inr, w.day_count,
       w.source, w.source_rank, w.adj_basis, w.computed_at
FROM   market.weekly_bar w
JOIN   market.weekly_bar_source_choice c
       ON c.security_id = w.security_id
      AND c.source      = w.source;

COMMENT ON VIEW market.weekly_bar_resolved IS
    'One homogeneous series per security - a single source and a single return '
    'basis across the entire history. Never mixes sources within a security.';


-- Mark whether a weekly bar covers a finished week.
--
-- The resampler dated the current partial week at its Friday, so an as_of of
-- 2026-08-11 (Monday) produced 4,052 bars stamped 2026-08-14 - three days in the
-- future, holding two sessions of data but compared as though complete.
ALTER TABLE market.weekly_bar
    ADD COLUMN IF NOT EXISTS is_complete boolean NOT NULL DEFAULT true;
ALTER TABLE market.weekly_bar
    ADD COLUMN IF NOT EXISTS last_trade_date date;

COMMENT ON COLUMN market.weekly_bar.is_complete IS
    'False when week_end_date is beyond the data cutoff - a partial week. '
    'Partial bars are excluded from technical analysis and parity comparison.';
COMMENT ON COLUMN market.weekly_bar.last_trade_date IS
    'The actual last session inside the week, which is what the bar really '
    'closes on. Differs from week_end_date on a short or partial week.';
