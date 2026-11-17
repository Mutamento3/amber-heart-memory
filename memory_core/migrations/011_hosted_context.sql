-- Owner-managed persona shares the existing space configuration revision.
ALTER TABLE memory_core.space_policies ADD COLUMN persona text NOT NULL DEFAULT ''
    CHECK (length(persona) <= 100000);

-- Reports of use are distinct from injection and semantic card edits. The
-- existing semantic timestamp trigger deliberately ignores these columns.
ALTER TABLE memory_core.memory_items ADD COLUMN used_count bigint NOT NULL DEFAULT 0
    CHECK (used_count >= 0);
ALTER TABLE memory_core.memory_items ADD COLUMN last_used_at text;

-- Receipt deduplication searches every version at the same timeline position.
CREATE INDEX conversation_versions_by_position ON memory_core.conversation_turns(bot_id,position_id,id);
