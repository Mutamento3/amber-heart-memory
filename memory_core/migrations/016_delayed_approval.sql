-- Preserve each installed space's previous choice; new spaces default to on.
ALTER TABLE memory_core.space_policies DROP CONSTRAINT space_policies_review_mode_check;
ALTER TABLE memory_core.space_policies ALTER COLUMN review_mode DROP DEFAULT;
ALTER TABLE memory_core.space_policies ALTER COLUMN review_mode TYPE boolean USING (review_mode='auto');
ALTER TABLE memory_core.space_policies RENAME COLUMN review_mode TO auto_approve_enabled;
ALTER TABLE memory_core.space_policies ALTER COLUMN auto_approve_enabled SET DEFAULT true;

ALTER TABLE memory_core.pending_memories ADD COLUMN review_ready_at text;
-- Upgrading must not immediately consume an existing person's waiting period.
UPDATE memory_core.pending_memories SET review_ready_at=memory_core.format_timestamp(CURRENT_TIMESTAMP)
WHERE status='pending';
ALTER TABLE memory_core.pending_memories ADD CONSTRAINT pending_review_clock
    CHECK (status<>'pending' OR review_ready_at IS NOT NULL);
CREATE INDEX pending_approval_clock ON memory_core.pending_memories(bot_id,review_ready_at)
WHERE status='pending';
