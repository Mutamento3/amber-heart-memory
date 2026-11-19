-- Spaces can hide the period section of recent notes. Existing content is
-- kept; new and upgraded spaces default to on.
ALTER TABLE memory_core.space_policies ADD COLUMN period_notes_enabled boolean NOT NULL DEFAULT true;
