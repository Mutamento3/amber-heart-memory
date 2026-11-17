-- Older active jobs have no reliable start time. A future retry starts a new measurement.
ALTER TABLE memory_core.import_jobs
    ADD COLUMN run_started_at text,
    ADD COLUMN run_processed_before bigint NOT NULL DEFAULT 0 CHECK (run_processed_before >= 0);
