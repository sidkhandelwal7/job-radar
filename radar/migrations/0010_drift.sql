-- Persist source drift so health/alarms can name the broken adapter, not just count it.
ALTER TABLE company_sources ADD COLUMN last_drift_at TEXT;
ALTER TABLE company_sources ADD COLUMN drift_note TEXT;
