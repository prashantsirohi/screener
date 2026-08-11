-- Derive the source choice from reconciliation, instead of mutating source_rank.
--
-- The demotion for a security whose bhavcopy history does not reconcile was
-- previously applied by UPDATEing source_rank to 40. That put a derived decision
-- inside mutable data: reloading weekly_bar reset every rank to its default and
-- silently re-promoted 44,669 bars from histories known not to reconcile.
--
-- The choice is now a pure function of what is in the store - bars available and
-- the latest reconciliation verdict - so it is recomputed on every read and no
-- reload can lose it.

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
           -- A history that fails to reconcile is demoted below the fallback:
           -- Yahoo is demonstrably better adjusted for those names, because it
           -- is carrying a corporate action our own adjustment is missing.
           CASE
               WHEN w.source = 'nse_bhavcopy'
                    AND v.verdict IN ('missed_action', 'disagree')
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
    'The single source elected to represent each security, derived from bar '
    'coverage and the latest reconciliation verdict. Recomputed on read, so a '
    'reload of weekly_bar cannot lose the decision.';

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
