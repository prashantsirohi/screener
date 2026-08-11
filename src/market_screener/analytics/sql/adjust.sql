-- Build the split/bonus-adjusted daily series.
--
-- cum_adj_factor for a given bar is the product of the adjustment factors of
-- every corporate action that takes effect AFTER that bar. Bars on or after the
-- last action have a factor of 1.0, so the most recent prices are unadjusted and
-- match what a quote screen shows today.
--
-- This is a PRICE-return basis. Yahoo's adjclose is TOTAL return - it also
-- strips dividends - so the two series diverge on high-yield names by the
-- cumulative yield. `adj_basis` records which one a row is on.
--
-- Parameters: $as_of (date)

WITH candidate_actions AS (
    SELECT security_id, ex_date, adjustment_factor, source,
           CASE source
               WHEN 'nse_api'          THEN 0    -- authoritative
               WHEN 'yahoo_divergence' THEN 1    -- corroborated by a second source
               ELSE 2                            -- single-source inference
           END AS source_priority
    FROM   src_corporate_action
    WHERE  adjustment_factor IS NOT NULL
      AND  adjustment_factor > 0
      AND  adjustment_factor <> 1.0
      AND  ex_date <= $as_of
      -- 'unconfirmed' is a shallow price gap that could equally be a bad day.
      -- Applying it would corrupt the security's whole history, so it is held
      -- back until the corporate-actions feed corroborates it.
      AND  confidence <> 'unconfirmed'
),
-- Defensive dedup. The same event can be recorded twice with slightly different
-- ex-dates - the feed gives the exact day, the divergence method only localises
-- it to a week. Applying both multiplies the factors together: SPORTKING's 1:10
-- split was recorded at 2024-09-09 and 2024-09-13 and 0.1 x 0.1 = 0.01 was being
-- applied to every prior bar, putting its whole history out by 10x.
--
-- Within a 7-day window per security, only the highest-priority row survives.
actions AS (
    SELECT a.security_id, a.ex_date, a.adjustment_factor
    FROM   candidate_actions a
    WHERE  NOT EXISTS (
        SELECT 1 FROM candidate_actions b
        WHERE  b.security_id = a.security_id
          AND  b.ex_date BETWEEN a.ex_date - 7 AND a.ex_date + 7
          AND  (b.source_priority < a.source_priority
                OR (b.source_priority = a.source_priority AND b.ex_date < a.ex_date))
    )
),
bars AS (
    SELECT security_id, trade_date, open, high, low, close, volume
    FROM   src_price_daily
    WHERE  trade_date <= $as_of
),
factors AS (
    SELECT
        b.security_id,
        b.trade_date,
        COALESCE(EXP(SUM(LN(a.adjustment_factor))), 1.0) AS cum_adj_factor
    FROM bars b
    LEFT JOIN actions a
           ON a.security_id = b.security_id
          AND a.ex_date     > b.trade_date
    GROUP BY b.security_id, b.trade_date
)
SELECT
    b.security_id,
    b.trade_date,
    b.open  * f.cum_adj_factor                                   AS adj_open,
    b.high  * f.cum_adj_factor                                   AS adj_high,
    b.low   * f.cum_adj_factor                                   AS adj_low,
    b.close * f.cum_adj_factor                                   AS adj_close,
    -- Volume moves inversely: a 1:2 split doubles the share count.
    CAST(b.volume / NULLIF(f.cum_adj_factor, 0) AS BIGINT)       AS adj_volume,
    f.cum_adj_factor,
    'split_bonus'                                                AS adj_basis
FROM bars b
JOIN factors f
  ON f.security_id = b.security_id AND f.trade_date = b.trade_date
