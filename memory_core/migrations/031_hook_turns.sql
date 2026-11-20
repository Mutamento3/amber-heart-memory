-- Hook identities are scoped to the space and independent of proxy request IDs.
ALTER TABLE memory_core.conversation_turns
    ADD COLUMN hook_session text CHECK (hook_session ~ '^[0-9a-f]{64}$'),
    ADD COLUMN hook_prompt text CHECK (
        hook_prompt IS NULL OR (hook_prompt ~ '^[0-9a-f]{64}$' AND hook_session IS NOT NULL)
    );

CREATE UNIQUE INDEX conversation_hook_prompt
    ON memory_core.conversation_turns(bot_id, hook_session, hook_prompt)
    WHERE hook_prompt IS NOT NULL AND history_state <> 'replaced' AND content_deleted_at IS NULL;
CREATE INDEX conversation_hook_history
    ON memory_core.conversation_turns(bot_id, hook_session, business_date, position_id)
    WHERE hook_session IS NOT NULL AND content_deleted_at IS NULL;
CREATE INDEX conversation_hook_pending
    ON memory_core.conversation_turns(bot_id)
    WHERE hook_session IS NOT NULL AND generation_state='pending' AND content_deleted_at IS NULL;
