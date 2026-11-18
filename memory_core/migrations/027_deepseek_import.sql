ALTER TABLE memory_core.import_jobs
    DROP CONSTRAINT import_jobs_source_check,
    ADD CONSTRAINT import_jobs_source_check
        CHECK (source IN ('chatgpt','claude','gemini','deepseek','kelivo','memory'));
