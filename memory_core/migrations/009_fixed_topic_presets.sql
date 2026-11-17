-- Keys are bound using the packaged template file, never a second list of descriptions.
ALTER TABLE memory_core.topics ADD COLUMN preset_key text;
WITH presets AS (
    SELECT template.key AS template, item.value AS topic
    FROM jsonb_each(__TOPIC_PRESETS__::jsonb) template
    CROSS JOIN LATERAL jsonb_array_elements(template.value) item
)
UPDATE memory_core.topics t SET preset_key=presets.topic->>'key'
FROM memory_core.space_policies p, presets
WHERE t.bot_id=p.bot_id AND p.topic_template=presets.template
    AND t.name=presets.topic->>'name';

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM memory_core.topics WHERE preset_key IS NULL) THEN
        RAISE EXCEPTION '旧主题与模板名称不一致，升级已停止；合成预览可重建，已有数据须先明确整理主题';
    END IF;
END $$;
ALTER TABLE memory_core.topics ALTER COLUMN preset_key SET NOT NULL,
    ADD CONSTRAINT topics_preset_key_format CHECK (preset_key ~ '^[a-z][a-z0-9_]*$'),
    ADD CONSTRAINT topics_space_preset_unique UNIQUE (bot_id,preset_key);

-- Review warnings concern participant identity only. Keep historical snapshots intact.
CREATE OR REPLACE FUNCTION memory_core.configuration_snapshot(target_space text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
SET search_path=pg_catalog,public,memory_core AS $function$
    SELECT jsonb_build_object(
        'participants', (SELECT jsonb_object_agg(r.role,jsonb_build_object('name',e.name,'aliases',e.aliases))
            FROM (VALUES ('user',p.user_entity_id),('assistant',p.assistant_entity_id)) r(role,id)
            JOIN memory_core.entities e ON e.id=r.id AND e.bot_id=p.bot_id))
    FROM memory_core.space_policies p WHERE p.bot_id=target_space;
$function$;
ALTER TABLE memory_core.topics DROP COLUMN description;

CREATE FUNCTION memory_core.lock_topic_preset()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,public,memory_core AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION '空间主题由模板固定，不能删除';
    END IF;
    IF ROW(NEW.bot_id,NEW.name,NEW.preset_key,NEW.is_fallback)
        IS DISTINCT FROM ROW(OLD.bot_id,OLD.name,OLD.preset_key,OLD.is_fallback) THEN
        RAISE EXCEPTION '空间主题由模板固定，不能修改';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION memory_core.lock_topic_preset() FROM PUBLIC;
CREATE TRIGGER lock_topic_preset BEFORE UPDATE OR DELETE ON memory_core.topics
    FOR EACH ROW EXECUTE FUNCTION memory_core.lock_topic_preset();

CREATE FUNCTION memory_core.lock_space_template()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,public,memory_core AS $$
BEGIN
    IF NEW.topic_template IS DISTINCT FROM OLD.topic_template THEN
        RAISE EXCEPTION '主题模板在创建空间后锁定，不能切换';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION memory_core.lock_space_template() FROM PUBLIC;
CREATE TRIGGER lock_space_template BEFORE UPDATE OF topic_template ON memory_core.space_policies
    FOR EACH ROW EXECUTE FUNCTION memory_core.lock_space_template();
