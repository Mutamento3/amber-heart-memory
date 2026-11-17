ALTER TABLE memory_core.pending_memories DROP CONSTRAINT pending_memories_status_check;
ALTER TABLE memory_core.pending_memories
    ADD CONSTRAINT pending_memories_status_check CHECK (status IN ('structure_pending','structure_failed','pending','rejected','approved')),
    ADD COLUMN review_key text CHECK (review_key IS NULL OR btrim(review_key)<>''),
    ADD COLUMN memory_item_id integer,
    ADD COLUMN duplicate_memory_id integer,
    ADD COLUMN reviewed_at text,
    ADD COLUMN dedup_state text CHECK (dedup_state IN ('clear','duplicate','failed_open')),
    ADD CONSTRAINT reviewed_card_space FOREIGN KEY (memory_item_id,bot_id) REFERENCES memory_core.memory_items(id,bot_id),
    ADD CONSTRAINT duplicate_card_space FOREIGN KEY (duplicate_memory_id,bot_id) REFERENCES memory_core.memory_items(id,bot_id),
    ADD CONSTRAINT reviewed_card_unique UNIQUE(memory_item_id),
    ADD CONSTRAINT review_request_unique UNIQUE(bot_id,review_key),
    ADD CHECK ((status='approved')=(memory_item_id IS NOT NULL)),
    ADD CHECK (status<>'approved' OR (review_key IS NOT NULL AND reviewed_at IS NOT NULL AND payload IS NOT NULL)),
    ADD CHECK (duplicate_memory_id IS NULL OR (status='rejected' AND dedup_state='duplicate'));
CREATE INDEX pending_memories_duplicate_source ON memory_core.pending_memories(bot_id,duplicate_memory_id) WHERE duplicate_memory_id IS NOT NULL;

ALTER TABLE memory_core.entities
    ADD CONSTRAINT entity_subtype_allowed CHECK (entity_subtype IS NULL OR entity_subtype IN (
        'persona','health_condition','body_part','plan','rule','kink','tech_system','artifact','belief','identity_trait','medication')),
    ADD CONSTRAINT entity_subtype_type CHECK (
        (entity_subtype<>'persona' OR entity_type='PERSON') AND
        (entity_subtype NOT IN ('artifact','medication') OR entity_type='OBJECT') AND
        (entity_subtype NOT IN ('belief','identity_trait','rule','kink') OR entity_type='CONCEPT'));
ALTER TABLE memory_core.memory_items
    ADD CONSTRAINT card_valence_range CHECK (valence BETWEEN -1 AND 1),
    ADD CONSTRAINT card_arousal_range CHECK (arousal BETWEEN 0 AND 1);
