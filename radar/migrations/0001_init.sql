-- Job Radar — initial schema.
-- Conventions: timestamps are ISO-8601 UTC strings; JSON columns end in _json; booleans are 0/1.
-- Nothing here is ever DELETEd by application code except dead-letter rows after review.

-------------------------------------------------------------------------------
-- Runs: one row per invocation of fetch / rescore / verify / notify / etc.
-------------------------------------------------------------------------------
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,                -- fetch | rescore | verify_links | delist_sweep | notify | digest | backfill | ...
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
    host            TEXT,                         -- laptop | actions
    stats_json      TEXT NOT NULL DEFAULT '{}',   -- sources_fetched, rows_new, rows_changed, bytes, etc.
    llm_calls       INTEGER NOT NULL DEFAULT 0,
    llm_models_json TEXT NOT NULL DEFAULT '{}',   -- {"<model>": <calls>}
    error           TEXT
);

-------------------------------------------------------------------------------
-- Companies and their ATS sources (registry, mirrored from companies.yaml + learned facts)
-------------------------------------------------------------------------------
CREATE TABLE companies (
    id                      INTEGER PRIMARY KEY,
    slug                    TEXT NOT NULL UNIQUE,     -- registry key, e.g. 'capital-one'
    name                    TEXT NOT NULL,
    aliases_json            TEXT NOT NULL DEFAULT '[]',
    tier                    INTEGER,                  -- 1 | 2 | 3
    is_dream_list           INTEGER NOT NULL DEFAULT 0,
    floor_exempt            INTEGER NOT NULL DEFAULT 0,
    is_quant_trading_firm   INTEGER NOT NULL DEFAULT 0,
    target_category         TEXT,                     -- big_tech_swe | bank_and_exchange_tech | ...
    tags_json               TEXT NOT NULL DEFAULT '[]',
    hq_metro                TEXT,
    -- company facts (enrichment, §8.6)
    headcount               INTEGER,
    stage                   TEXT,                     -- public | late_stage | series_b | ...
    funding_usd             INTEGER,
    is_public               INTEGER,
    offices_json            TEXT NOT NULL DEFAULT '[]',
    layoff_history_json     TEXT NOT NULL DEFAULT '[]',
    rto_policy              TEXT,
    alumni_presence    TEXT,                     -- strong | some | unknown
    -- learned
    hiring_velocity         REAL,                     -- new postings / week, rolling
    median_days_to_close    REAL,                     -- learned from delist watcher
    repost_rate             REAL,
    comp_observations_json  TEXT NOT NULL DEFAULT '[]',
    referral_likelihood     TEXT,                     -- likely | possible | unknown
    notes_md                TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE company_sources (
    id                      INTEGER PRIMARY KEY,
    company_id              INTEGER NOT NULL REFERENCES companies(id),
    provider                TEXT NOT NULL,            -- greenhouse | workday | oracle | lever | ashby | workable | smartrecruiters | recruitee | github
    slug                    TEXT NOT NULL,            -- provider-specific locator (workday: tenant/wdN/site)
    careers_url             TEXT,
    cadence                 TEXT NOT NULL DEFAULT 'hourly',   -- 15min | hourly | 6h | daily
    enabled                 INTEGER NOT NULL DEFAULT 1,
    detected_at             TEXT,                     -- when auto-detected (null if hand-written)
    last_fetched_at         TEXT,
    last_success_at         TEXT,
    last_row_count          INTEGER,
    typical_row_count       REAL,                     -- EWMA, for drift alarms
    etag                    TEXT,
    last_modified           TEXT,
    consecutive_empty       INTEGER NOT NULL DEFAULT 0,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    circuit_open_until      TEXT,
    last_error              TEXT,
    UNIQUE (provider, slug)
);
CREATE INDEX idx_company_sources_company ON company_sources(company_id);

-------------------------------------------------------------------------------
-- Raw payloads: append-only, gzipped on disk, addressed by sha256
-------------------------------------------------------------------------------
CREATE TABLE raw_payloads (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER REFERENCES runs(id),
    source_id       INTEGER REFERENCES company_sources(id),
    provider        TEXT NOT NULL,
    slug            TEXT NOT NULL,
    url             TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    http_status     INTEGER,
    sha256          TEXT NOT NULL,
    byte_size       INTEGER NOT NULL,
    path            TEXT NOT NULL,                -- relative to data/raw
    row_count       INTEGER,
    unchanged       INTEGER NOT NULL DEFAULT 0,   -- 1 if sha256 identical to previous payload for this source
    kind            TEXT NOT NULL DEFAULT 'list'  -- list | detail | departments | aggregator
);
CREATE INDEX idx_raw_payloads_source ON raw_payloads(source_id, fetched_at);
CREATE INDEX idx_raw_payloads_sha ON raw_payloads(sha256);

-------------------------------------------------------------------------------
-- Postings: the master list. Natural key (source_provider, source_slug, source_job_id).
-------------------------------------------------------------------------------
CREATE TABLE postings (
    id                      INTEGER PRIMARY KEY,
    cluster_id              INTEGER,
    -- identity
    source                  TEXT NOT NULL,            -- company_direct | aggregator | third_party
    source_provider         TEXT NOT NULL,
    source_slug             TEXT NOT NULL,
    source_job_id           TEXT NOT NULL,
    apply_url               TEXT NOT NULL,
    canonical_url           TEXT,
    url_last_verified_at    TEXT,
    url_status              TEXT NOT NULL DEFAULT 'unverified',  -- live | redirected | dead | unverified
    url_verify_method       TEXT,                     -- api | http | source_presence
    url_final               TEXT,                     -- where apply_url resolved to at last check
    raw_payload_ref         INTEGER REFERENCES raw_payloads(id),
    detail_payload_ref      INTEGER REFERENCES raw_payloads(id),
    content_hash            TEXT,                     -- sha256 of normalized fields; drives change detection
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    delisted_at             TEXT,
    repost_of_id            INTEGER REFERENCES postings(id),
    repost_count            INTEGER NOT NULL DEFAULT 0,
    changed_since_first_seen INTEGER NOT NULL DEFAULT 0,
    -- core
    company_name            TEXT NOT NULL,
    company_id              INTEGER REFERENCES companies(id),
    title                   TEXT NOT NULL,
    title_normalized        TEXT,
    description_md          TEXT,
    description_fetched     INTEGER NOT NULL DEFAULT 0,
    department              TEXT,
    team                    TEXT,
    employment_type         TEXT,                     -- full_time | part_time | contract | internship | unknown
    posted_at               TEXT,
    updated_at_source       TEXT,
    application_deadline    TEXT,
    start_date              TEXT,
    locations_json          TEXT NOT NULL DEFAULT '[]',   -- [{"raw":..,"metro":..,"city":..,"state":..,"country":..}]
    metros_json             TEXT NOT NULL DEFAULT '[]',   -- ["new_york","washington_dc"]
    primary_metro           TEXT,
    country_codes_json      TEXT NOT NULL DEFAULT '[]',
    is_international_only   INTEGER NOT NULL DEFAULT 0,
    is_multiple_locations   INTEGER NOT NULL DEFAULT 0,
    work_mode               TEXT,                     -- onsite | hybrid | remote | unknown
    remote_eligible         INTEGER,
    days_in_office          INTEGER,
    requires_clearance      INTEGER,
    requires_advanced_degree INTEGER,
    min_years_experience    REAL,
    max_years_experience    REAL,
    sponsorship             TEXT,                     -- offers | does_not_offer | unknown
    graduation_window       TEXT,
    -- comp
    base_posted_min         REAL,
    base_posted_max         REAL,
    base_posted_currency    TEXT,
    base_posted_interval    TEXT,                     -- year | hour | month
    base_est                REAL,
    base_est_low            REAL,
    base_est_high           REAL,
    signing_est             REAL,
    bonus_target_pct_est    REAL,
    equity_type             TEXT,                     -- none | rsu_public | rsu_private | options | unknown
    equity_annual_est       REAL,
    tc_year1_est            REAL,
    comp_source             TEXT,                     -- posted_range | ashby_posted | pay_transparency | company_recent | lca_prior | peer_model | unknown
    comp_confidence         REAL,
    base_col_adjusted       REAL,
    base_after_tax_est      REAL,
    tax_delta_vs_baseline   REAL,
    location_utility_premium REAL,
    effective_value         REAL,
    real_terms_vs_baseline  REAL,
    -- derived
    in_scope                INTEGER,
    scope_reason            TEXT,
    role_family             TEXT,
    role_subfamily          TEXT,
    seniority               TEXT,
    is_new_grad             INTEGER,
    is_stretch              INTEGER NOT NULL DEFAULT 0,  -- 1-2 YoE stretch req
    target_category         TEXT,
    program_type            TEXT,
    tech_tags_json          TEXT NOT NULL DEFAULT '[]',
    industry_tags_json      TEXT NOT NULL DEFAULT '[]',
    company_tier            INTEGER,
    is_dream_list           INTEGER NOT NULL DEFAULT 0,
    referral_likelihood     TEXT,
    referral_secured        INTEGER NOT NULL DEFAULT 0,
    same_market_as_baseline_offer INTEGER NOT NULL DEFAULT 0,
    comp_score              REAL,
    career_capital_score    REAL,
    fit_score               REAL,
    winnability_score       REAL,
    location_score          REAL,
    culture_score           REAL,
    composite_score         REAL,
    beats_baseline               TEXT,                     -- clearly_better | arguably_better | worse
    beats_baseline_reason        TEXT,
    beats_baseline_decomposition_json TEXT,
    floor_result            TEXT,                     -- pass | fail | exempt
    floor_fail_reasons_json TEXT NOT NULL DEFAULT '[]',
    hard_blockers_json      TEXT NOT NULL DEFAULT '[]',
    urgency_score           REAL,
    apply_priority_rank     INTEGER,
    priority                REAL,
    ev_estimate             REAL,
    p_offer                 REAL,
    prep_archetype          TEXT,
    prep_hours_est          REAL,
    matched_strengths_json  TEXT NOT NULL DEFAULT '[]',
    gaps_json               TEXT NOT NULL DEFAULT '[]',
    requirements_json       TEXT,
    parse_confidence        REAL,
    score_version           TEXT,
    scored_at               TEXT,
    score_explanation_json  TEXT,
    queue_action            TEXT,                     -- apply_today | apply_this_week | watch | get_referral_first | blocked_needs_prep
    -- workflow (operator-owned; never auto-mutated by fetch/score)
    status                  TEXT NOT NULL DEFAULT 'new',   -- new | shortlisted | applied | dismissed | snoozed
    status_changed_at       TEXT,
    dismiss_reason          TEXT,
    snooze_until            TEXT,
    starred                 INTEGER NOT NULL DEFAULT 0,
    tags_user_json          TEXT NOT NULL DEFAULT '[]',
    notes_md                TEXT,
    override_floor          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_provider, source_slug, source_job_id)
);
CREATE INDEX idx_postings_company ON postings(company_id);
CREATE INDEX idx_postings_cluster ON postings(cluster_id);
CREATE INDEX idx_postings_status ON postings(status);
CREATE INDEX idx_postings_delisted ON postings(delisted_at);
CREATE INDEX idx_postings_last_seen ON postings(last_seen_at);
CREATE INDEX idx_postings_first_seen ON postings(first_seen_at);
CREATE INDEX idx_postings_priority ON postings(priority);
CREATE INDEX idx_postings_composite ON postings(composite_score);
CREATE INDEX idx_postings_url_status ON postings(url_status, url_last_verified_at);
CREATE INDEX idx_postings_block ON postings(company_id, title_normalized, primary_metro);

-- Full-text search over title + description + company.
CREATE VIRTUAL TABLE postings_fts USING fts5(
    title, description_md, company_name,
    content='postings', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER postings_ai AFTER INSERT ON postings BEGIN
    INSERT INTO postings_fts(rowid, title, description_md, company_name)
    VALUES (new.id, new.title, new.description_md, new.company_name);
END;
CREATE TRIGGER postings_ad AFTER DELETE ON postings BEGIN
    INSERT INTO postings_fts(postings_fts, rowid, title, description_md, company_name)
    VALUES ('delete', old.id, old.title, old.description_md, old.company_name);
END;
CREATE TRIGGER postings_au AFTER UPDATE OF title, description_md, company_name ON postings BEGIN
    INSERT INTO postings_fts(postings_fts, rowid, title, description_md, company_name)
    VALUES ('delete', old.id, old.title, old.description_md, old.company_name);
    INSERT INTO postings_fts(rowid, title, description_md, company_name)
    VALUES (new.id, new.title, new.description_md, new.company_name);
END;

-------------------------------------------------------------------------------
-- Append-only audit of everything that happens to a posting
-------------------------------------------------------------------------------
CREATE TABLE posting_events (
    id              INTEGER PRIMARY KEY,
    posting_id      INTEGER NOT NULL REFERENCES postings(id),
    run_id          INTEGER REFERENCES runs(id),
    event_type      TEXT NOT NULL,    -- first_seen | changed | delisted | relisted | repost_detected | link_checked | scored | status_changed | note | applied | referral_logged | ...
    at              TEXT NOT NULL,
    data_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_posting_events_posting ON posting_events(posting_id, at);

CREATE TABLE posting_clusters (
    id                      INTEGER PRIMARY KEY,
    canonical_posting_id    INTEGER REFERENCES postings(id),
    size                    INTEGER NOT NULL DEFAULT 1,
    method                  TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE link_checks (
    id              INTEGER PRIMARY KEY,
    posting_id      INTEGER NOT NULL REFERENCES postings(id),
    checked_at      TEXT NOT NULL,
    url             TEXT NOT NULL,
    method          TEXT NOT NULL,    -- api | http | source_presence
    status          TEXT NOT NULL,    -- live | redirected | dead | unverified
    http_status     INTEGER,
    final_url       TEXT,
    reason          TEXT
);
CREATE INDEX idx_link_checks_posting ON link_checks(posting_id, checked_at);

-------------------------------------------------------------------------------
-- Applications: first-class, permanent. Never deleted, never hidden by a filter.
-------------------------------------------------------------------------------
CREATE TABLE applications (
    id                      INTEGER PRIMARY KEY,
    posting_id              INTEGER REFERENCES postings(id),   -- nullable: manual entry
    company_name            TEXT NOT NULL,
    title                   TEXT NOT NULL,
    location                TEXT,
    apply_url               TEXT,
    applied_at              TEXT NOT NULL,
    stage                   TEXT NOT NULL DEFAULT 'applied',   -- applied | oa_pending | oa_done | screen | onsite | offer | rejected | ghosted | withdrawn
    stage_changed_at        TEXT NOT NULL,
    completed               INTEGER NOT NULL DEFAULT 0,
    outcome                 TEXT,
    referral_used           INTEGER NOT NULL DEFAULT 0,
    referral_contact        TEXT,
    follow_up_due           TEXT,
    first_response_at       TEXT,
    notes_md                TEXT,
    base_offered            REAL,
    source_of_discovery     TEXT,                     -- provider or aggregator repo or 'manual'
    target_category         TEXT,
    resume_version_used     TEXT,
    created_manually        INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX idx_applications_posting ON applications(posting_id);
CREATE INDEX idx_applications_stage ON applications(stage);

CREATE TABLE application_events (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    at              TEXT NOT NULL,
    event_type      TEXT NOT NULL,    -- created | stage_changed | note | follow_up | completed | ...
    data_json       TEXT NOT NULL DEFAULT '{}'
);

-------------------------------------------------------------------------------
-- Saved filters, notifications, config versions, dead letters
-------------------------------------------------------------------------------
CREATE TABLE saved_filters (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    query           TEXT NOT NULL,
    is_preset       INTEGER NOT NULL DEFAULT 0,
    alert_tier      TEXT,             -- p0 | p1 | null
    sort            TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER REFERENCES runs(id),
    posting_id      INTEGER REFERENCES postings(id),
    cluster_id      INTEGER,
    tier            TEXT NOT NULL,    -- p0 | p1 | p2 | p3
    trigger         TEXT NOT NULL,
    channel         TEXT NOT NULL,    -- telegram | ios_shortcut | email | ics
    sent_at         TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    external_id     TEXT,             -- e.g. telegram message id
    engaged         INTEGER,          -- null = unknown, 1 = acted on, 0 = ignored
    engaged_at      TEXT,
    engagement      TEXT              -- shortlisted | applied | dismissed | opened | ignored
);
CREATE INDEX idx_notifications_posting ON notifications(posting_id);
CREATE INDEX idx_notifications_sent ON notifications(sent_at);

CREATE TABLE config_versions (
    id              INTEGER PRIMARY KEY,
    sha256          TEXT NOT NULL,
    yaml_text       TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE dead_letters (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER REFERENCES runs(id),
    provider        TEXT,
    slug            TEXT,
    raw_payload_ref INTEGER REFERENCES raw_payloads(id),
    error           TEXT NOT NULL,
    item_json       TEXT,
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT,
    resolution      TEXT
);

CREATE TABLE kv (
    key             TEXT PRIMARY KEY,
    value_json      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
