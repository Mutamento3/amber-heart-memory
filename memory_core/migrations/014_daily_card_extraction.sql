-- Historical migrations remain immutable. Do not silently relabel old data.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM memory_core.source_materials WHERE source_kind<>'mainline')
       OR EXISTS (SELECT 1 FROM memory_core.memory_items WHERE source_kind NOT IN ('mainline','gardener')) THEN
        RAISE EXCEPTION USING ERRCODE='P4001', MESSAGE='Unsupported legacy material sources';
    END IF;
END $$;

ALTER TABLE memory_core.space_policies
    ADD COLUMN review_mode text NOT NULL DEFAULT 'auto' CHECK (review_mode IN ('auto','manual'));
ALTER TABLE memory_core.daily_tasks
    ADD COLUMN model_usage jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(model_usage)='array');

ALTER TABLE memory_core.source_materials DROP CONSTRAINT source_materials_source_kind_check;
ALTER TABLE memory_core.source_materials ADD CONSTRAINT source_materials_source_kind_check CHECK (source_kind='mainline');
ALTER TABLE memory_core.memory_items DROP CONSTRAINT memory_items_source_kind_check;
ALTER TABLE memory_core.memory_items ADD CONSTRAINT memory_items_source_kind_check CHECK (source_kind IN ('mainline','gardener'));
