-- Recent notes are distinct from the per-turn frozen recall snapshot.
CREATE TABLE memory_core.recent_notes (
    bot_id text PRIMARY KEY REFERENCES memory_core.space_policies(bot_id),
    period text NOT NULL DEFAULT '',
    health text NOT NULL DEFAULT '',
    longterm text NOT NULL DEFAULT '',
    daily text NOT NULL DEFAULT '',
    version bigint NOT NULL DEFAULT 0 CHECK (version>=0),
    tidy_job jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(tidy_job)='object'),
    updated_at text NOT NULL DEFAULT memory_core.format_timestamp(CURRENT_TIMESTAMP)
);

-- Only pending bookmarks exist here; publishing and withdrawing delete rows.
CREATE TABLE memory_core.moment_bookmarks (
    id uuid PRIMARY KEY,
    bot_id text NOT NULL REFERENCES memory_core.space_policies(bot_id),
    section text NOT NULL CHECK (section IN ('period','health','longterm','daily')),
    content text NOT NULL CHECK (btrim(content)<>'' AND content !~ E'[\\r\\n]'),
    business_date text NOT NULL CHECK (business_date ~ '^\d{4}-\d{2}-\d{2}$'),
    created_at text NOT NULL DEFAULT memory_core.format_timestamp(CURRENT_TIMESTAMP)
);
CREATE INDEX moment_bookmarks_due ON memory_core.moment_bookmarks(bot_id,business_date);
REVOKE ALL ON memory_core.recent_notes,memory_core.moment_bookmarks FROM PUBLIC;
