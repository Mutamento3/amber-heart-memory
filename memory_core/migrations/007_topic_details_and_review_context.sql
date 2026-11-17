-- Reuse the description column introduced in version 3; retain any existing text.
UPDATE memory_core.topics SET description='' WHERE description IS NULL;
ALTER TABLE memory_core.topics ALTER COLUMN description SET DEFAULT '',
    ALTER COLUMN description SET NOT NULL;
ALTER TABLE memory_core.pending_memories ADD COLUMN policy_snapshot jsonb
    CHECK (policy_snapshot IS NULL OR jsonb_typeof(policy_snapshot)='object');

-- Store only review-relevant configuration. Private background facts remain a digest.
CREATE FUNCTION memory_core.configuration_snapshot(target_space text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
SET search_path=pg_catalog,public,memory_core AS $function$
    SELECT jsonb_build_object(
        'participants', (SELECT jsonb_object_agg(r.role,jsonb_build_object('name',e.name,'aliases',e.aliases))
            FROM (VALUES ('user',p.user_entity_id),('assistant',p.assistant_entity_id)) r(role,id)
            JOIN memory_core.entities e ON e.id=r.id AND e.bot_id=p.bot_id),
        'topics', (SELECT coalesce(jsonb_agg(jsonb_build_object('id',t.id,'name',t.name,'description',t.description)
            ORDER BY t.id),'[]'::jsonb) FROM memory_core.topics t WHERE t.bot_id=p.bot_id),
        'ignored_terms',p.ignored_terms,
        'source_retention_days',p.source_retention_days,
        'extraction_rules',jsonb_build_object('minimum_importance',p.minimum_importance,
            'daily_limit',p.daily_limit,'background_digest',md5(p.core_facts::text)))
    FROM memory_core.space_policies p WHERE p.bot_id=target_space;
$function$;
REVOKE ALL ON FUNCTION memory_core.configuration_snapshot(text) FROM PUBLIC;

-- Only equal revisions can truthfully be reconstructed from the current configuration.
UPDATE memory_core.pending_memories d SET policy_snapshot=memory_core.configuration_snapshot(d.bot_id)
FROM memory_core.space_policies p WHERE p.bot_id=d.bot_id AND p.revision=d.policy_revision;
