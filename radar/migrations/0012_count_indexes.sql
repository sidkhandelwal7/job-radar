-- Health/ops counters were full scans of a 700 MB table (3 s each, worse while a cycle writes).
CREATE INDEX IF NOT EXISTS idx_postings_in_scope ON postings(in_scope) WHERE in_scope = 1;
CREATE INDEX IF NOT EXISTS idx_postings_has_req ON postings(has_requirements) WHERE has_requirements = 1;
CREATE INDEX IF NOT EXISTS idx_postings_raw_hash_null ON postings(id) WHERE raw_hash IS NULL;
