ALTER TABLE memory_core.pending_memories DROP CONSTRAINT pending_memories_status_check;
ALTER TABLE memory_core.pending_memories ADD CONSTRAINT pending_memories_status_check
    CHECK (status IN ('structure_pending','structure_failed','pending','rejected','approved','deleted'));
-- Keep the row for the daily quota and extraction idempotency, never a card copy.
ALTER TABLE memory_core.pending_memories ADD CONSTRAINT deleted_draft_has_no_copy
    CHECK (status<>'deleted' OR (narrative='{}'::jsonb AND payload IS NULL
        AND memory_item_id IS NULL AND duplicate_memory_id IS NULL));
