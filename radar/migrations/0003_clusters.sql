-- Cluster canonical flag: default views show one row per cluster (the canonical one); siblings
-- remain fully queryable (filters are views).
ALTER TABLE postings ADD COLUMN is_cluster_canonical INTEGER NOT NULL DEFAULT 1;
ALTER TABLE postings ADD COLUMN cluster_size INTEGER NOT NULL DEFAULT 1;
ALTER TABLE postings ADD COLUMN url_key TEXT;               -- normalized URL identity for dedupe
CREATE INDEX idx_postings_url_key ON postings(url_key);
CREATE INDEX idx_postings_canonical ON postings(is_cluster_canonical);
