-- Explicit rescore flag. The previous "scored_at < last_seen_at" rule re-scored every row a full
-- scan merely *saw* (121k rows per catch-up cycle); now only new/changed/delisted/relisted/
-- workflow-touched rows are due.
ALTER TABLE postings ADD COLUMN needs_rescore INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_postings_needs_rescore ON postings(needs_rescore) WHERE needs_rescore = 1;
