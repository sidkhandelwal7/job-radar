-- Employers seen in aggregator feeds but missing from the registry → queued for slug detection.
CREATE TABLE discovery_queue (
    id                  INTEGER PRIMARY KEY,
    company_name_norm   TEXT NOT NULL UNIQUE,
    company_name        TEXT NOT NULL,
    example_url         TEXT,
    company_url         TEXT,
    seen_count          INTEGER NOT NULL DEFAULT 1,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',   -- pending | resolved | unresolvable | ignored
    detected_provider   TEXT,
    detected_slug       TEXT,
    resolved_company_id INTEGER REFERENCES companies(id),
    note                TEXT
);
