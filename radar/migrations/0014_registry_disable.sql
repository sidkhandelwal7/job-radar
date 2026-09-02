-- Registry audit (D62): disables carry a reason and a timestamp so (a) sync_registry does not
-- silently re-enable them from the YAML on the next cycle, and (b) the nightly can re-probe
-- low-yield sources after 14 days as the undo.
ALTER TABLE company_sources ADD COLUMN disabled_reason TEXT;
ALTER TABLE company_sources ADD COLUMN disabled_at TEXT;
