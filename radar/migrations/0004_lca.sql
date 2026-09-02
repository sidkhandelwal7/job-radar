-- DOL OFLC LCA disclosure data (free, quarterly). A PRIOR, never truth: certified filings only,
-- entry-level clusters at Wage Level I, the filed wage is a floor not an offer (§6d).
CREATE TABLE lca_wages (
    id              INTEGER PRIMARY KEY,
    fiscal_file     TEXT NOT NULL,            -- e.g. FY2026_Q3
    employer_name   TEXT NOT NULL,
    employer_norm   TEXT NOT NULL,
    job_title       TEXT,
    soc_code        TEXT,
    worksite_city   TEXT,
    worksite_state  TEXT,
    metro           TEXT,
    wage_level      TEXT,                     -- I | II | III | IV
    wage_annual     REAL NOT NULL,            -- offered wage (from), annualized
    prevailing_annual REAL,
    decision_date   TEXT
);
CREATE INDEX idx_lca_employer ON lca_wages(employer_norm, soc_code);
CREATE INDEX idx_lca_metro ON lca_wages(metro, soc_code, wage_level);
