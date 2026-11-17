CREATE TABLE memory_web.space_access_keys (
    key_id text PRIMARY KEY CHECK (key_id ~ '^[0-9a-f]{32}$'),
    bot_id text NOT NULL REFERENCES memory_core.space_policies(bot_id),
    ciphertext text NOT NULL CHECK (ciphertext<>''),
    created_at text NOT NULL DEFAULT memory_core.format_timestamp(CURRENT_TIMESTAMP),
    revoked_at text
);
CREATE UNIQUE INDEX space_one_access_key ON memory_web.space_access_keys(bot_id) WHERE revoked_at IS NULL;
REVOKE ALL ON memory_web.space_access_keys FROM PUBLIC;
