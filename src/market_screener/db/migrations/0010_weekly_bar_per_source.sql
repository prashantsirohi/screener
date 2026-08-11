-- Keep weekly bars per source, and resolve the winner in a view.
--
-- The original PK (security_id, week_end_date) meant a bhavcopy bar overwrote
-- the Yahoo bar for the same week. That makes the displacement invisible and,
-- worse, leaves nothing to reconcile against - the whole point of carrying two
-- sources is to be able to ask whether they agree, which is how a missed
-- corporate action gets caught.
--
-- Both rows are now retained; `weekly_bar_resolved` picks the highest
-- source_rank per week. Readers use the view, so the change is transparent to
-- everything downstream.

ALTER TABLE market.weekly_bar DROP CONSTRAINT weekly_bar_pkey;
ALTER TABLE market.weekly_bar
    ADD CONSTRAINT weekly_bar_pkey PRIMARY KEY (security_id, week_end_date, source);

CREATE OR REPLACE VIEW market.weekly_bar_resolved AS
SELECT DISTINCT ON (security_id, week_end_date)
       security_id, week_end_date, iso_year, iso_week,
       open, high, low, close, volume, turnover_inr, day_count,
       source, source_rank, adj_basis, computed_at
FROM   market.weekly_bar
ORDER  BY security_id, week_end_date, source_rank DESC, computed_at DESC;

COMMENT ON VIEW market.weekly_bar_resolved IS
    'One bar per security-week: the highest-ranked source available. bhavcopy '
    'is rank 100, Yahoo 50, so the backfill displaces Yahoo per security with no '
    'cutover event while both rows remain for reconciliation.';
