-- Widen weekly price columns so a stored bar round-trips exactly.
--
-- Yahoo returns float32 values widened to float64, so a close arrives as
-- 16.12999916076660. numeric(18,6) truncated that to 16.129999, a ~4.7e-7 loss.
--
-- Economically that is nothing. But `overhead_supply` buckets each bar on
-- `typical_price > current_price`, and a bar sitting almost exactly on the
-- current price flips bucket on a 1e-7 nudge - one such bar moved AAKASH's
-- overhead supply by 0.3pp. Threshold comparisons make sub-tick noise
-- observable, so the store should not introduce any.
--
-- 10 decimal places round-trips a float64 price exactly at Indian equity
-- magnitudes, which removes the whole class of knife-edge divergence rather
-- than papering over it with a per-field tolerance.

-- The views read these columns, so Postgres refuses the type change until they
-- are dropped. Recreated identically below.
DROP VIEW IF EXISTS market.weekly_bar_resolved;
DROP VIEW IF EXISTS market.weekly_bar_source_choice;

ALTER TABLE market.weekly_bar
    ALTER COLUMN open  TYPE numeric(24, 10),
    ALTER COLUMN high  TYPE numeric(24, 10),
    ALTER COLUMN low   TYPE numeric(24, 10),
    ALTER COLUMN close TYPE numeric(24, 10);

CREATE VIEW market.weekly_bar_source_choice AS
SELECT DISTINCT ON (security_id)
       security_id, source, source_rank, adj_basis,
       count(*)           OVER (PARTITION BY security_id, source) AS bars,
       min(week_end_date) OVER (PARTITION BY security_id, source) AS first_week,
       max(week_end_date) OVER (PARTITION BY security_id, source) AS last_week
FROM   market.weekly_bar
ORDER  BY security_id,
          source_rank DESC,
          count(*)           OVER (PARTITION BY security_id, source) DESC,
          max(week_end_date) OVER (PARTITION BY security_id, source) DESC,
          source;

CREATE VIEW market.weekly_bar_resolved AS
SELECT w.security_id, w.week_end_date, w.iso_year, w.iso_week,
       w.open, w.high, w.low, w.close, w.volume, w.turnover_inr, w.day_count,
       w.source, w.source_rank, w.adj_basis,
       w.is_complete, w.last_trade_date,
       w.computed_at
FROM   market.weekly_bar w
JOIN   market.weekly_bar_source_choice c
       ON c.security_id = w.security_id
      AND c.source      = w.source;

ALTER TABLE market.price_daily_adj
    ALTER COLUMN adj_open  TYPE numeric(24, 10),
    ALTER COLUMN adj_high  TYPE numeric(24, 10),
    ALTER COLUMN adj_low   TYPE numeric(24, 10),
    ALTER COLUMN adj_close TYPE numeric(24, 10);
