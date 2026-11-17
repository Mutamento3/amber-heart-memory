-- Restoration starts a fresh inactivity period without rewriting usage history.
ALTER TABLE memory_core.memory_items ADD COLUMN restored_at text;
CREATE INDEX memory_items_archive_month ON memory_core.memory_items(bot_id,archived_at)
    WHERE archive_reason IS NOT NULL;
