ALTER TABLE memory_core.conversation_turns
    ADD COLUMN summary_progress jsonb CHECK (summary_progress IS NULL OR jsonb_typeof(summary_progress)='object');

ALTER TABLE memory_core.conversation_space_state
    ADD COLUMN summary_attempt jsonb CHECK (summary_attempt IS NULL OR jsonb_typeof(summary_attempt)='object');
