ALTER TABLE memory_core.import_jobs
    DROP CONSTRAINT import_jobs_source_check,
    ADD CONSTRAINT import_jobs_source_check
        CHECK (source IN ('chatgpt','claude','gemini','kelivo','memory'));

ALTER TABLE memory_core.import_items
    ADD COLUMN assistant_key text,
    ADD COLUMN assistant_name text,
    ADD COLUMN is_kelivo_memory boolean NOT NULL DEFAULT false,
    ALTER COLUMN business_date DROP NOT NULL,
    ADD CONSTRAINT import_items_date_required_for_chat
        CHECK (business_date IS NOT NULL OR is_kelivo_memory);

ALTER TABLE memory_core.source_materials
    ALTER COLUMN business_date DROP NOT NULL,
    ADD CONSTRAINT source_materials_date_required_for_chat
        CHECK (business_date IS NOT NULL OR material_kind = 'memory');

ALTER TABLE memory_core.pending_memories
    ALTER COLUMN business_date DROP NOT NULL;
