-- Record the ratio STEP separately from the overall difference.
--
-- Comparing a price-return series against a total-return one, a steady
-- divergence is just dividends and carries no information about correctness.
-- A sudden step between adjacent weeks is the useful signal: it means one series
-- applied a corporate action the other did not.
--
-- Keeping the two apart stops a dividend payer from looking like a data defect.

ALTER TABLE market.price_source_reconciliation
    ADD COLUMN IF NOT EXISTS max_step_pct numeric(12, 6);

COMMENT ON COLUMN market.price_source_reconciliation.max_step_pct IS
    'Largest week-over-week jump in the bhavcopy/Yahoo close ratio. A step above '
    '~10% indicates a corporate action missing from one of the two series.';

COMMENT ON COLUMN market.price_source_reconciliation.max_abs_diff_pct IS
    'Largest absolute divergence between the two series. Expected to grow going '
    'back on dividend payers (total return vs price return); not an error signal '
    'on its own.';

COMMENT ON COLUMN market.price_source_reconciliation.weeks_matching IS
    'Of the most recent 13 overlapping weeks, how many agree within 1%. Both '
    'bases coincide at the latest bar, so recent weeks are the meaningful test.';
