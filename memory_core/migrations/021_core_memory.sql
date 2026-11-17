-- Preserve the complete text. Oversized legacy values stop the migration
-- atomically instead of silently truncating somebody's memory.
-- An upgrade may span several versions in one transaction. Flush deferred FK
-- checks before changing this table, including checks queued by the text copy.
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE memory_core.space_policies ADD COLUMN core_memory text NOT NULL DEFAULT '';
UPDATE memory_core.space_policies SET core_memory=array_to_string(core_facts,E'\n');
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE memory_core.space_policies
    ADD CONSTRAINT core_memory_length CHECK (char_length(core_memory)<=5000),
    DROP COLUMN core_facts;
