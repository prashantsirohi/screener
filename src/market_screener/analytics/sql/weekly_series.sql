-- Export the weekly series the technical layer consumes.
--
-- Two guarantees, both load-bearing:
--
-- 1. ONE return basis for the whole run. Yahoo's adjclose is total return and
--    the bhavcopy series is price return. Splicing them steps a series by the
--    cumulative dividend yield, and any moving average spanning the seam is
--    averaging two different quantities - it can invent or erase a stage
--    transition. 1,406 of 2,086 active securities were mixed before this.
--
-- 2. ONE source per security WITHIN that basis, elected by coverage. Even on a
--    single basis, two providers must not be interleaved week by week.
--
-- Complete weeks only: src_weekly_bar_all already excludes partial bars, so the
-- current unfinished week cannot drag the latest MA point or the 52-week high.
--
-- This does the bulk scan and the election, and nothing else. No moving
-- averages, no slopes, no relative strength - those live in domain/weinstein.py.
--
-- Parameters: $as_of (date), $adj_basis (text)

WITH filtered AS (
    SELECT *
    FROM   src_weekly_bar_all
    WHERE  week_end_date <= $as_of
      AND  adj_basis = $adj_basis
),
coverage AS (
    SELECT security_id,
           source,
           COUNT(*)              AS bars,
           MAX(week_end_date)    AS last_week
    FROM   filtered
    GROUP  BY security_id, source
),
elected AS (
    SELECT security_id, source,
           ROW_NUMBER() OVER (PARTITION BY security_id
                              ORDER BY bars DESC, last_week DESC, source) AS rn
    FROM   coverage
)
SELECT f.security_id,
       f.week_end_date,
       f.open,
       f.high,
       f.low,
       f.close,
       f.volume,
       f.turnover_inr,
       f.source,
       f.adj_basis,
       f.last_trade_date
FROM   filtered f
JOIN   elected e
       ON e.security_id = f.security_id
      AND e.source      = f.source
      AND e.rn          = 1
ORDER  BY f.security_id, f.week_end_date
