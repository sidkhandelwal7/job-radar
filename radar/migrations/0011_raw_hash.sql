-- Cheap change detection: hash of the adapter-level fields, so an unchanged job on a full scan
-- costs one dict comparison instead of a full title/location/comp re-parse (was ~18 ms/job,
-- 2 h for a 400k-row catch-up cycle).
ALTER TABLE postings ADD COLUMN raw_hash TEXT;
