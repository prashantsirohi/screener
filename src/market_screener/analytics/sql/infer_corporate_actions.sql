-- Infer split/bonus corporate actions from a close-to-close discontinuity.
--
-- IMPORTANT, verified empirically against 3 years of bhavcopy: NSE's
-- PRVSCLSGPRIC is the RAW previous close, NOT restated onto the post-action
-- basis. Across 269 confirmed large gaps, prev_close/prior_close was exactly
-- 1.000 in every single case. An earlier version of this query assumed the
-- opposite and therefore found almost nothing.
--
-- The signal is the close itself. SIGACHI on 2023-10-09 went 408.90 -> 39.50 on
-- a 1:10 split, so
--
--     factor = close[t] / close[t-1]
--
-- is the adjustment factor, give or take the day's genuine price move.
--
-- Separating a split from a crash:
--   * Indian equities are circuit-limited, so an ordinary session cannot move
--     more than 20%. $min_move defaults well above that.
--   * A split lands on a simple ratio - 1:2, 1:5, 1:10, 2:3. A crash does not.
--     Candidates whose factor is not within $snap_tol of a small fraction are
--     rejected, which is what keeps genuine collapses out of the table.
--
-- GAP SAFETY: LAG() reaches across holes in the history. A partial backfill once
-- produced 1,761 phantom events by comparing a 2023 price to a 2026 one.
-- $max_gap_days restricts the comparison to consecutive sessions.
--
-- Parameters: $as_of (date), $min_move (double, e.g. 0.28),
--             $max_gap_days (int, e.g. 5), $snap_tol (double, e.g. 0.05)

WITH seq AS (
    SELECT
        p.security_id,
        p.trade_date,
        p.close,
        p.volume,
        LAG(p.close)      OVER w AS prior_close,
        LAG(p.trade_date) OVER w AS prior_date,
        LAG(p.volume)     OVER w AS prior_volume
    FROM src_price_daily p
    WHERE p.trade_date <= $as_of
      AND p.close > 0
    WINDOW w AS (PARTITION BY p.security_id ORDER BY p.trade_date)
),
gaps AS (
    SELECT
        security_id,
        trade_date AS ex_date,
        prior_date,
        prior_close,
        close,
        volume,
        prior_volume,
        close / NULLIF(prior_close, 0) AS factor
    FROM seq
    WHERE prior_close IS NOT NULL
      AND prior_date IS NOT NULL
      AND prior_close > 0
      AND DATE_DIFF('day', prior_date, trade_date) <= $max_gap_days
      AND ABS(1.0 - close / NULLIF(prior_close, 0)) >= $min_move
),
snapped AS (
    SELECT
        g.*,
        -- Nearest simple ratio. Splits/bonuses cluster on these; crashes do not.
        r.ratio                       AS snap_ratio,
        ABS(g.factor - r.ratio)
            / NULLIF(r.ratio, 0)      AS snap_error
    FROM gaps g
    CROSS JOIN LATERAL (
        SELECT ratio
        FROM (VALUES
            (0.0100), (0.0200), (0.0500),          -- 1:100, 1:50, 1:20 (ETF splits)
            (0.1000), (0.1250), (0.2000), (0.2500),-- 1:10, 1:8, 1:5, 1:4
            (0.3333), (0.4000), (0.5000),          -- 1:3, 2:5, 1:2
            (0.6000), (0.6667), (0.7500),          -- 3:5, 2:3, 3:4
            (1.5000), (2.0000), (2.5000),          -- reverse splits
            (3.0000), (4.0000), (5.0000), (10.0000)
        ) AS v(ratio)
        ORDER BY ABS(g.factor - ratio) / NULLIF(ratio, 0)
        LIMIT 1
    ) r
)
SELECT
    security_id,
    ex_date,
    prior_date,
    prior_close,
    close,
    factor,
    snap_ratio,
    snap_error,
    ROUND(1.0 / NULLIF(snap_ratio, 0), 2)   AS implied_ratio,
    CASE WHEN snap_ratio < 1 THEN 'split_or_bonus' ELSE 'consolidation' END AS action_type,
    ABS(1.0 - factor)                       AS move_size,
    volume,
    prior_volume
FROM snapped
-- A genuine action lands close to its ratio; the residual is just that day's
-- ordinary price move. A crash sits nowhere near one.
WHERE snap_error <= $snap_tol
ORDER BY security_id, ex_date
