-- Demote only securities with a genuinely missing corporate action.
--
-- The source election previously demoted both `missed_action` and `disagree`.
-- Measured across the universe, `disagree` is dividend yield rather than a data
-- defect:
--
--   verdict         n     median div yield   series diff   step >= 10%
--   agree        1747              0.13%          1.2%     -
--   disagree      115              1.69%          5.6%     0 of 115
--   drift          57              1.97%          6.4%     -
--   missed_action  35              0.00%         21.8%     yes, on a clean ratio
--
-- Yahoo's series is total return and bhavcopy's is price return, so a dividend
-- payer is *expected* to diverge. Demoting on that basis moved 115 securities
-- off a perfectly good exchange-sourced history - Coal India, Wipro, PTC - and
-- on the price-return basis excluded them from the screen entirely.
--
-- Only a step landing on a ratio a split or bonus produces is evidence the
-- adjustment has missed something.

DROP VIEW IF EXISTS market.weekly_bar_resolved;
DROP VIEW IF EXISTS market.weekly_bar_source_choice;

CREATE VIEW market.weekly_bar_source_choice AS
WITH latest_verdict AS (
    SELECT DISTINCT ON (security_id) security_id, verdict
    FROM   market.price_source_reconciliation
    ORDER  BY security_id, as_of_date DESC
),
ranked AS (
    SELECT w.security_id,
           w.source,
           w.source_rank,
           w.adj_basis,
           CASE
               WHEN w.source = 'nse_bhavcopy' AND v.verdict = 'missed_action'
               THEN w.source_rank - 60
               ELSE w.source_rank
           END                          AS effective_rank,
           count(*)                     AS bars,
           min(w.week_end_date)         AS first_week,
           max(w.week_end_date)         AS last_week
    FROM   market.weekly_bar w
    LEFT JOIN latest_verdict v ON v.security_id = w.security_id
    GROUP  BY w.security_id, w.source, w.source_rank, w.adj_basis, v.verdict
)
SELECT DISTINCT ON (security_id)
       security_id, source, source_rank, effective_rank, adj_basis,
       bars, first_week, last_week
FROM   ranked
ORDER  BY security_id, effective_rank DESC, bars DESC, last_week DESC, source;

COMMENT ON VIEW market.weekly_bar_source_choice IS
    'The single source elected per security. Demotes bhavcopy only where a '
    'corporate action is demonstrably missing (missed_action); dividend-driven '
    'divergence between a total-return and a price-return series is expected '
    'and is not a defect.';

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
