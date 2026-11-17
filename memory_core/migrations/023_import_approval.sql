-- Existing jobs keep their recorded review behavior, including after a retry.
ALTER TABLE memory_core.import_jobs ADD COLUMN auto_approve boolean NOT NULL DEFAULT false;
