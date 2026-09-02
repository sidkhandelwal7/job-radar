-- Notification state: per-company escalation, callback dedupe, and digest bookkeeping.
ALTER TABLE notifications ADD COLUMN dedupe_key TEXT;          -- e.g. "p0:cluster:123" — never alert twice on one cluster per tier
ALTER TABLE notifications ADD COLUMN reason TEXT;              -- strongest single reason shown in the payload
ALTER TABLE notifications ADD COLUMN quiet_hours_override INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_notifications_dedupe ON notifications(dedupe_key);
CREATE INDEX idx_notifications_tier_sent ON notifications(tier, sent_at);

CREATE TABLE notify_company_state (
    company_key         TEXT PRIMARY KEY,              -- company_id or normalized name
    consecutive_ignored INTEGER NOT NULL DEFAULT 0,
    demoted_to          TEXT,                          -- 'p2' when three consecutive P1s were ignored
    demoted_at          TEXT,
    note                TEXT
);

CREATE TABLE telegram_updates (
    update_id           INTEGER PRIMARY KEY,
    received_at         TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    applied             INTEGER NOT NULL DEFAULT 0
);
