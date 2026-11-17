-- Upgrade the original one-row installation without editing the v1 baseline.
ALTER TABLE memory_core.installation DROP CONSTRAINT installation_version_check;
ALTER TABLE memory_core.installation ADD CHECK (version >= 1);
CREATE UNIQUE INDEX installation_singleton ON memory_core.installation ((true));

CREATE TABLE memory_core.schema_migrations (
    version integer PRIMARY KEY CHECK (version >= 1),
    migration_sha256 text NOT NULL,
    installed_at text NOT NULL
);
REVOKE ALL ON memory_core.schema_migrations FROM PUBLIC;

CREATE OR REPLACE FUNCTION memory_core.format_timestamp(instant timestamptz)
RETURNS text LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = pg_catalog, public, memory_core AS $$
DECLARE
    zone text;
    local_time timestamp;
    offset_seconds integer;
BEGIN
    SELECT settings->>'timezone' INTO STRICT zone FROM memory_core.installation;
    local_time := instant AT TIME ZONE zone;
    offset_seconds := extract(epoch FROM (local_time - (instant AT TIME ZONE 'UTC')))::integer;
    RETURN to_char(local_time, 'YYYY-MM-DD"T"HH24:MI:SS')
        || CASE WHEN offset_seconds >= 0 THEN '+' ELSE '-' END
        || lpad((abs(offset_seconds) / 3600)::text, 2, '0') || ':'
        || lpad(((abs(offset_seconds) % 3600) / 60)::text, 2, '0');
END $$;
