-- D64: start-date compatibility is its own hard gate, separate from years-of-experience.
-- Extracted deterministically from the description; the body beats the title.
ALTER TABLE postings ADD COLUMN start_flag TEXT;        -- incompatible | compatible | NULL (no signal)
ALTER TABLE postings ADD COLUMN start_evidence TEXT;    -- the phrase that decided it
