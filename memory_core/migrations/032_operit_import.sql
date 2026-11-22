ALTER TABLE memory_core.import_jobs
    DROP CONSTRAINT import_jobs_source_check,
    ADD CONSTRAINT import_jobs_source_check
        CHECK (source IN ('chatgpt','claude','gemini','deepseek','kelivo','operit','memory'));

ALTER TABLE memory_core.import_items RENAME COLUMN is_kelivo_memory TO is_memory_entry;
