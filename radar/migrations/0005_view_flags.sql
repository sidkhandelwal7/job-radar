-- Precomputed default-view membership so filtered views are index lookups, not 7 table scans.
ALTER TABLE postings ADD COLUMN in_default_view INTEGER NOT NULL DEFAULT 1;
ALTER TABLE postings ADD COLUMN suppressed_reason TEXT;        -- first matching suppression rule, for the "why" breakdown
CREATE INDEX idx_postings_view ON postings(in_default_view, priority);
CREATE INDEX idx_postings_suppressed ON postings(suppressed_reason);
CREATE INDEX idx_postings_facets ON postings(in_default_view, beats_baseline, primary_metro, target_category, company_name, work_mode, status, queue_action, is_dream_list);
CREATE INDEX idx_postings_rank ON postings(apply_priority_rank);
