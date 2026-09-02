-- Narrow the postings table: big text moves to posting_docs so scans/facets never touch it.
-- (A 200k-row table with inline descriptions makes every COUNT/GROUP BY an I/O storm.)
CREATE TABLE posting_docs (
    posting_id                  INTEGER PRIMARY KEY REFERENCES postings(id),
    title                       TEXT,          -- denormalized copies for the FTS external-content table
    company_name                TEXT,
    description_md              TEXT,
    score_explanation_json      TEXT,
    beats_baseline_decomposition_json TEXT,
    requirements_json           TEXT
);
INSERT INTO posting_docs (posting_id, title, company_name, description_md, score_explanation_json, beats_baseline_decomposition_json, requirements_json)
    SELECT id, title, company_name, description_md, score_explanation_json, beats_baseline_decomposition_json, requirements_json FROM postings;

DROP TRIGGER IF EXISTS postings_ai;
DROP TRIGGER IF EXISTS postings_ad;
DROP TRIGGER IF EXISTS postings_au;
DROP TABLE IF EXISTS postings_fts;

ALTER TABLE postings DROP COLUMN description_md;
ALTER TABLE postings DROP COLUMN score_explanation_json;
ALTER TABLE postings DROP COLUMN beats_baseline_decomposition_json;
ALTER TABLE postings DROP COLUMN requirements_json;

-- small derived columns that used to live inside the JSON blobs (needed by query fields / presets)
ALTER TABLE postings ADD COLUMN est_days_to_close REAL;
ALTER TABLE postings ADD COLUMN first_drop INTEGER NOT NULL DEFAULT 0;
ALTER TABLE postings ADD COLUMN tax_rate REAL;
ALTER TABLE postings ADD COLUMN has_requirements INTEGER NOT NULL DEFAULT 0;
ALTER TABLE postings ADD COLUMN has_llm_fit INTEGER NOT NULL DEFAULT 0;
UPDATE postings SET has_requirements = 1 WHERE id IN (SELECT posting_id FROM posting_docs WHERE requirements_json IS NOT NULL);
UPDATE postings SET has_llm_fit = 1 WHERE id IN (SELECT posting_id FROM posting_docs WHERE json_extract(requirements_json, '$.llm_fit') IS NOT NULL);

CREATE VIRTUAL TABLE postings_fts USING fts5(
    title, description_md, company_name,
    content='posting_docs', content_rowid='posting_id', tokenize='porter unicode61'
);
CREATE TRIGGER posting_docs_ai AFTER INSERT ON posting_docs BEGIN
    INSERT INTO postings_fts(rowid, title, description_md, company_name)
    VALUES (new.posting_id, new.title, new.description_md, new.company_name);
END;
CREATE TRIGGER posting_docs_ad AFTER DELETE ON posting_docs BEGIN
    INSERT INTO postings_fts(postings_fts, rowid, title, description_md, company_name)
    VALUES ('delete', old.posting_id, old.title, old.description_md, old.company_name);
END;
CREATE TRIGGER posting_docs_au AFTER UPDATE OF title, description_md, company_name ON posting_docs BEGIN
    INSERT INTO postings_fts(postings_fts, rowid, title, description_md, company_name)
    VALUES ('delete', old.posting_id, old.title, old.description_md, old.company_name);
    INSERT INTO postings_fts(rowid, title, description_md, company_name)
    VALUES (new.posting_id, new.title, new.description_md, new.company_name);
END;
INSERT INTO postings_fts(postings_fts) VALUES ('rebuild');
