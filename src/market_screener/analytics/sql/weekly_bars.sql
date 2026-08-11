-- Resample the adjusted daily series to weekly bars, W-FRI.
--
-- week_end_date is the Friday of the ISO week containing the session, ALWAYS -
-- including for a week that ends early on a holiday. Yahoo stamps its weekly
-- bars at week-start (Monday), so without pinning both sources to the same
-- Friday the stock/benchmark join used for relative strength silently drops
-- nearly every row and RS comes back NULL rather than erroring.
--
-- Emitted at source_rank 100 so these displace the imported Yahoo bars (rank 50)
-- per security, with no cutover event.
--
-- Parameters: $as_of (date), $min_days (int - drop stub weeks below this)

WITH daily AS (
    SELECT
        a.security_id,
        a.trade_date,
        -- date_trunc('week') is Monday in DuckDB; +4 days lands on Friday.
        CAST(DATE_TRUNC('week', a.trade_date) + INTERVAL 4 DAY AS DATE) AS week_end_date,
        a.adj_open, a.adj_high, a.adj_low, a.adj_close, a.adj_volume,
        p.turnover_inr
    FROM src_price_daily_adj a
    LEFT JOIN src_price_daily p
           ON p.security_id = a.security_id AND p.trade_date = a.trade_date
    WHERE a.trade_date <= $as_of
      AND a.adj_close IS NOT NULL
),
ordered AS (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY security_id, week_end_date
                           ORDER BY trade_date)        AS rn_first,
        ROW_NUMBER() OVER (PARTITION BY security_id, week_end_date
                           ORDER BY trade_date DESC)   AS rn_last
    FROM daily
)
SELECT
    security_id,
    week_end_date,
    CAST(EXTRACT(isoyear FROM week_end_date) AS SMALLINT) AS iso_year,
    CAST(EXTRACT(week    FROM week_end_date) AS SMALLINT) AS iso_week,
    MAX(CASE WHEN rn_first = 1 THEN adj_open END)         AS open,
    MAX(adj_high)                                         AS high,
    MIN(adj_low)                                          AS low,
    MAX(CASE WHEN rn_last  = 1 THEN adj_close END)        AS close,
    SUM(adj_volume)                                       AS volume,
    SUM(turnover_inr)                                     AS turnover_inr,
    CAST(COUNT(*) AS SMALLINT)                            AS day_count,
    'nse_bhavcopy'                                        AS source,
    CAST(100 AS SMALLINT)                                 AS source_rank,
    'split_bonus'                                         AS adj_basis,
    -- A week is only complete once its Friday has actually passed. A Monday
    -- as_of otherwise emits a bar stamped the coming Friday, holding one or two
    -- sessions but treated downstream as a full week - it distorts the latest
    -- MA point, the 52-week high and every parity comparison.
    (week_end_date <= $as_of)                             AS is_complete,
    MAX(trade_date)                                       AS last_trade_date
FROM ordered
GROUP BY security_id, week_end_date
HAVING COUNT(*) >= $min_days
ORDER BY security_id, week_end_date
