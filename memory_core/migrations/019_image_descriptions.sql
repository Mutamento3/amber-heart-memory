ALTER TABLE memory_core.conversation_messages ADD COLUMN image_descriptions jsonb
    CHECK (image_descriptions IS NULL OR
        (jsonb_typeof(image_descriptions)='array' AND role='user' AND content_deleted_at IS NULL));

CREATE FUNCTION memory_core.guard_message_image_descriptions()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public, memory_core AS $$
DECLARE image_count integer;
BEGIN
    IF TG_OP='UPDATE' AND OLD.image_descriptions IS NOT NULL
       AND NEW.image_descriptions IS DISTINCT FROM OLD.image_descriptions
       AND NOT (NEW.image_descriptions IS NULL AND NEW.content_parts IS NULL AND NEW.content_deleted_at IS NOT NULL) THEN
        RAISE EXCEPTION 'image_descriptions_are_final' USING ERRCODE='23514';
    END IF;
    IF NEW.image_descriptions IS NOT NULL THEN
        IF jsonb_typeof(NEW.image_descriptions)<>'array' THEN
            RAISE EXCEPTION 'image_descriptions_require_array' USING ERRCODE='23514';
        END IF;
        SELECT count(*) INTO image_count FROM jsonb_array_elements(NEW.content_parts) p WHERE p->>'type'='image_placeholder';
        IF image_count=0 OR jsonb_array_length(NEW.image_descriptions)<>image_count THEN
            RAISE EXCEPTION 'image_description_count_mismatch' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER message_image_descriptions_final
    BEFORE INSERT OR UPDATE OF image_descriptions,content_parts,content_deleted_at
    ON memory_core.conversation_messages
    FOR EACH ROW EXECUTE FUNCTION memory_core.guard_message_image_descriptions();
REVOKE ALL ON FUNCTION memory_core.guard_message_image_descriptions() FROM PUBLIC;
