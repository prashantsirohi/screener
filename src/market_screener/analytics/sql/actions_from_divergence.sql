-- Recover corporate actions from disagreement between the two price sources.
--
-- Yahoo's adjusted series already incorporates splits and bonuses. Where the
-- bhavcopy/Yahoo close ratio STEPS between two adjacent weeks, Yahoo applied an
-- adjustment that the bhavcopy-side inference missed - typically a shallow bonus
-- below the 28% close-gap threshold, which price alone cannot separate from an
-- ordinary bad day.
--
-- The step is the missing factor, and because it comes from an independent
-- source it is better evidence than the single-source inference: two providers
-- agreeing on a discontinuity is corroboration, not a guess.
--
-- Dividends also move the ratio, so only steps that land on a clean ratio are
-- accepted; a dividend produces a small, untidy number.
--
-- Parameters: $as_of (date), $min_step (double, e.g. 0.10),
--             $snap_tol (double, e.g. 0.04)

WITH paired AS (
    SELECT w.security_id, w.week_end_date,
           w.close / y.close AS ratio
    FROM   src_weekly_bar_all w
    JOIN   src_weekly_bar_all y
           ON y.security_id = w.security_id AND y.week_end_date = w.week_end_date
    WHERE  w.source = 'nse_bhavcopy' AND y.source = 'yahoo_weekly'
      AND  y.close > 0 AND w.close > 0
      AND  w.week_end_date <= $as_of
),
steps AS (
    SELECT security_id, week_end_date, ratio,
           LAG(ratio)         OVER w AS prev_ratio,
           LAG(week_end_date) OVER w AS prev_week,
           ratio / NULLIF(LAG(ratio) OVER w, 0) AS step
    FROM paired
    WINDOW w AS (PARTITION BY security_id ORDER BY week_end_date)
),
big AS (
    SELECT *
    FROM   steps
    WHERE  prev_ratio IS NOT NULL
      AND  step IS NOT NULL
      AND  ABS(1.0 - step) >= $min_step
      -- Adjacent weeks only; a hole in one series is not a corporate action.
      AND  DATE_DIFF('day', prev_week, week_end_date) <= 10
),
snapped AS (
    SELECT b.*, r.ratio AS snap_ratio,
           ABS(b.step - r.ratio) / NULLIF(r.ratio, 0) AS snap_error
    FROM big b
    CROSS JOIN LATERAL (
        SELECT ratio FROM (VALUES
            (0.0100),(0.0200),(0.0500),(0.1000),(0.1250),(0.2000),(0.2500),
            (0.3333),(0.4000),(0.5000),(0.6000),(0.6667),(0.7500),(0.8000),
            (1.2500),(1.3333),(1.5000),(2.0000),(2.5000),(3.0000),(4.0000),
            (5.0000),(10.0000)
        ) AS v(ratio)
        ORDER BY ABS(b.step - ratio) / NULLIF(ratio, 0)
        LIMIT 1
    ) r
)
SELECT security_id,
       week_end_date AS ex_week,
       prev_week,
       ratio,
       prev_ratio,
       step,
       snap_ratio,
       snap_error,
       CASE WHEN snap_ratio < 1 THEN 'split' ELSE 'consolidation' END AS action_type
FROM snapped
WHERE snap_error <= $snap_tol
ORDER BY security_id, week_end_date
