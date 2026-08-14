-- Retain the actual page, not just what the parser made of it.
--
-- `screener_page_raw` was named for an intent it did not fulfil. The HTTP body
-- was parsed the moment it arrived and only the resulting dict was stored, so
-- "raw" meant "raw parser output". The consequence is narrower than it sounds
-- but worse: `rebuild-facts` can replay the fact EXPLOSION over retained
-- payloads, but it cannot replay the PARSE. Anything the parser dropped -
-- a table it did not recognise, a row whose label changed - was gone the moment
-- the response was discarded, and no amount of re-running fixes it.
--
-- Forward-only by design. Backfilling means re-fetching 2,086 pages, and a bulk
-- refresh at volume is exactly what provoked the throttle that blanked 307 of
-- them. The 45-day staleness gate re-captures the corpus at ~25 pages/day
-- instead, so coverage fills in over roughly twelve weeks at no extra request
-- cost. Existing rows keep raw_html NULL, which is honest: we do not have it.

ALTER TABLE market.screener_page_raw
    -- gzipped utf-8 body. Compressed because the corpus is ~2,000 pages of
    -- 200-400KB HTML: ~600MB raw, ~60MB gzipped, and TOAST would compress it
    -- anyway - doing it explicitly makes the stored size predictable and lets
    -- the checksum describe the bytes we actually received.
    ADD COLUMN raw_html      bytea,
    -- SHA-256 of the UNCOMPRESSED body. Identifies the response independently
    -- of the compression settings used to store it, so a re-fetch that returns
    -- identical HTML is recognisable as such.
    ADD COLUMN raw_sha256    text,
    ADD COLUMN raw_bytes     integer,
    ADD COLUMN http_status   integer,
    ADD COLUMN content_type  text,
    -- The URL actually served, after redirects. screener.in redirects
    -- /company/X/consolidated/ to the standalone page when no consolidated
    -- statements exist, which silently changes the basis of everything parsed
    -- from it - source_url records what we asked for, this what we got.
    ADD COLUMN final_url     text;

COMMENT ON COLUMN market.screener_page_raw.raw_html IS
    'gzipped utf-8 response body; NULL for pages captured before migration 0017';
COMMENT ON COLUMN market.screener_page_raw.raw_sha256 IS
    'sha256 of the uncompressed body';
COMMENT ON COLUMN market.screener_page_raw.payload IS
    'parsed representation - a derived layer, not the source. Re-parsing needs raw_html';

-- Finding pages that predate retention, and pages whose parse can be replayed.
CREATE INDEX screener_page_raw_replayable_idx
    ON market.screener_page_raw (security_id, fetched_at DESC)
    WHERE raw_html IS NOT NULL;
