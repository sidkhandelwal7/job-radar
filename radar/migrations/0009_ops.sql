-- Phase 6: weekly velocity snapshots, application kits, calibration proposals, system alarms.
CREATE TABLE IF NOT EXISTS velocity_snapshots (
  id INTEGER PRIMARY KEY,
  week_start TEXT NOT NULL,              -- ISO date (Monday)
  company_id INTEGER,                    -- NULL = market-wide row
  company_name TEXT,
  open_reqs INTEGER NOT NULL DEFAULT 0,
  new_reqs INTEGER NOT NULL DEFAULT 0,
  closed_reqs INTEGER NOT NULL DEFAULT 0,
  in_scope_open INTEGER NOT NULL DEFAULT 0,
  median_days_to_close REAL,
  repost_rate REAL,
  taken_at TEXT NOT NULL,
  UNIQUE(week_start, company_id)
);
ALTER TABLE posting_docs ADD COLUMN kit_md TEXT;
ALTER TABLE posting_docs ADD COLUMN kit_at TEXT;
CREATE TABLE IF NOT EXISTS calibration_runs (
  id INTEGER PRIMARY KEY,
  month TEXT NOT NULL,                   -- YYYY-MM
  ran_at TEXT NOT NULL,
  labeled INTEGER NOT NULL,
  positives INTEGER NOT NULL,
  proposal_json TEXT NOT NULL,
  applied INTEGER NOT NULL DEFAULT 0     -- always 0: proposals only (§10)
);
