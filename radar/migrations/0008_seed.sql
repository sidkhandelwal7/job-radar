-- Per-source seed watermark: postings first seen during a source's first successful scan are new to
-- *us*, not new to the market, and must never trigger alerts (§20 anti-noise). Backfilled to the last
-- success (+15 min: the pre-0008 bookkeeping stamp preceded the upsert) so the existing backlog is seed.
ALTER TABLE company_sources ADD COLUMN seed_completed_at TEXT;
UPDATE company_sources SET seed_completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', last_success_at, '+15 minutes') WHERE last_success_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings(first_seen_at);
