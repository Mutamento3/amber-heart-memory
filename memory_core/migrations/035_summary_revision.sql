ALTER TABLE memory_core.conversation_summaries
    ADD COLUMN revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0);
