-- Recreate the resolved view with the completeness columns.
--
-- 0012 created the view before its own ALTER TABLE added is_complete and
-- last_trade_date, so the view was pinned to the old column list and the
-- analytics layer could not filter partial weeks.

-- DROP first: CREATE OR REPLACE cannot insert columns into the middle of an
-- existing view's column list.
DROP VIEW IF EXISTS market.weekly_bar_resolved;

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

COMMENT ON VIEW market.weekly_bar_resolved IS
    'One homogeneous series per security - a single source and a single return '
    'basis across the entire history. Never mixes sources within a security. '
    'Callers must still filter is_complete to exclude the current partial week.';
