-- Restrict physical deletion to previews and unfinished import work that
-- has produced no reviewable cards. The application role has no DELETE grant.
CREATE FUNCTION memory_core.discard_import_preview(target_space text, target_job uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, memory_core AS $$
BEGIN
    PERFORM 1 FROM memory_core.import_jobs
    WHERE bot_id=target_space AND id=target_job AND status='preview' AND model_calls=0
    FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM memory_core.import_items
        WHERE bot_id=target_space AND job_id=target_job AND extraction_id IS NOT NULL) THEN
        RETURN false;
    END IF;

    DELETE FROM memory_core.import_items WHERE bot_id=target_space AND job_id=target_job;
    DELETE FROM memory_core.import_jobs WHERE bot_id=target_space AND id=target_job;
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION memory_core.discard_import_preview(text, uuid) FROM PUBLIC;

CREATE FUNCTION memory_core.release_unfinished_import_materials(target_space text, target_job uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, memory_core AS $$
DECLARE
    item record;
BEGIN
    PERFORM 1 FROM memory_core.import_jobs
    WHERE bot_id=target_space AND id=target_job AND status='failed'
    FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM memory_core.import_items
        WHERE bot_id=target_space AND job_id=target_job AND status='running') THEN
        RETURN false;
    END IF;

    FOR item IN
        SELECT i.id, e.id AS extraction_id, e.material_id
        FROM memory_core.import_items i
        JOIN memory_core.material_extractions e ON (e.id,e.bot_id)=(i.extraction_id,i.bot_id)
        JOIN memory_core.source_materials s ON (s.id,s.bot_id,s.source_key)=(e.material_id,e.bot_id,i.source_key)
        WHERE i.bot_id=target_space AND i.job_id=target_job AND i.selected
          AND i.status IN ('pending','failed') AND e.status <> 'complete'
          AND i.source_key LIKE 'import:%' AND e.request_key='request:' || i.source_key
        ORDER BY e.material_id
    LOOP
        -- Match structure/review's source-before-draft lock order.
        PERFORM 1 FROM memory_core.source_materials
        WHERE bot_id=target_space AND id=item.material_id FOR UPDATE;
        PERFORM 1 FROM memory_core.material_extractions
        WHERE bot_id=target_space AND id=item.extraction_id AND status <> 'complete' FOR UPDATE;
        IF NOT FOUND THEN CONTINUE; END IF;
        PERFORM 1 FROM memory_core.pending_memories
        WHERE bot_id=target_space AND extraction_id=item.extraction_id ORDER BY id FOR UPDATE;

        IF EXISTS (SELECT 1 FROM memory_core.pending_memories
            WHERE bot_id=target_space AND extraction_id=item.extraction_id
              AND (review_ready_at IS NOT NULL OR status NOT IN ('structure_pending','structure_failed')))
          OR EXISTS (SELECT 1 FROM memory_core.import_items
            WHERE bot_id=target_space AND extraction_id=item.extraction_id AND id<>item.id) THEN
            CONTINUE;
        END IF;
        IF EXISTS (SELECT 1 FROM memory_core.pending_memories
            WHERE bot_id=target_space AND extraction_id=item.extraction_id
              AND structure_claim_until::timestamptz > clock_timestamp()) THEN
            RAISE lock_not_available USING MESSAGE = 'import_structure_in_progress';
        END IF;
        UPDATE memory_core.import_items SET extraction_id=NULL
        WHERE bot_id=target_space AND job_id=target_job AND id=item.id;
        DELETE FROM memory_core.pending_memories
        WHERE bot_id=target_space AND extraction_id=item.extraction_id;
        DELETE FROM memory_core.material_extractions WHERE bot_id=target_space AND id=item.extraction_id;
        DELETE FROM memory_core.source_materials WHERE bot_id=target_space AND id=item.material_id;
    END LOOP;
    RETURN true;
EXCEPTION WHEN lock_not_available THEN
    RETURN false;
END;
$$;
REVOKE ALL ON FUNCTION memory_core.release_unfinished_import_materials(text, uuid) FROM PUBLIC;
