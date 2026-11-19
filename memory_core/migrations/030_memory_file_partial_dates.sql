-- Self-organized memory files may give only a year or a month, or no date.
-- Chat material keeps exact days.
ALTER TABLE memory_core.import_items
    DROP CONSTRAINT import_items_business_date_check,
    ADD CONSTRAINT import_items_business_date_check
        CHECK (business_date ~ '^\d{4}-\d{2}-\d{2}$'
            OR (source_key LIKE 'import:memory:%' AND business_date ~ '^\d{4}(-\d{2})?$')),
    DROP CONSTRAINT import_items_date_required_for_chat,
    ADD CONSTRAINT import_items_date_required_for_chat
        CHECK (business_date IS NOT NULL OR is_kelivo_memory OR source_key LIKE 'import:memory:%');

ALTER TABLE memory_core.source_materials
    DROP CONSTRAINT source_materials_business_date_check,
    ADD CONSTRAINT source_materials_business_date_check
        CHECK (business_date ~ '^\d{4}-\d{2}-\d{2}$'
            OR (material_kind = 'memory' AND business_date ~ '^\d{4}(-\d{2})?$'));
