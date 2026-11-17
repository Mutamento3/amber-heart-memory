-- A calendar date has no time or timezone. Keep existing spaces explicitly unset.
ALTER TABLE memory_core.space_policies ADD COLUMN acquaintance_date text
    CHECK (acquaintance_date IS NULL OR (
        acquaintance_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        AND to_char(acquaintance_date::date,'YYYY-MM-DD')=acquaintance_date));

-- Preserve historical draft snapshots; only new captures include the new metadata.
CREATE OR REPLACE FUNCTION memory_core.configuration_snapshot(target_space text)
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
        'acquaintance_date',p.acquaintance_date,
        'extraction_rules',jsonb_build_object('minimum_importance',p.minimum_importance,
            'daily_limit',p.daily_limit,'background_digest',md5(p.core_facts::text)))
    FROM memory_core.space_policies p WHERE p.bot_id=target_space;
$function$;
