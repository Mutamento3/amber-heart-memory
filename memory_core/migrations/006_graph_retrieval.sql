-- Same graph scoring, relation-source quotas and hop whitelist as the
-- verified live function. Space/revision validation replaces private defaults.
CREATE FUNCTION memory_core.search_entities(query_embedding public.vector, target_bot_id text, match_count integer)
RETURNS TABLE(id integer,name text,entity_type text,entity_subtype text,description text,similarity double precision)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public,memory_core AS $$
    SELECT e.id,e.name,e.entity_type,e.entity_subtype,e.description,
        (1-(e.embedding <=> query_embedding))::double precision
    FROM memory_core.entities e WHERE e.bot_id=target_bot_id AND e.embedding IS NOT NULL
    ORDER BY e.embedding <=> query_embedding LIMIT greatest(0,match_count);
$$;

CREATE OR REPLACE FUNCTION memory_core.search_graph(
    seed_entity_ids INTEGER[],
    seed_topic_ids INTEGER[],
    target_bot_id TEXT,
    match_count INTEGER,
    target_item_kind TEXT,
    include_archived BOOLEAN,
    expected_revision BIGINT
)
RETURNS TABLE (
    memory_id INTEGER,
    raw_score DOUBLE PRECISION,
    graph_reason TEXT,
    graph_source TEXT,
    hop_count INTEGER,
    seed_entity_id INTEGER,
    seed_topic_id INTEGER,
    relation_type TEXT,
    neighbor_entity_id INTEGER
)
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, memory_core
AS $function$
#variable_conflict use_column
BEGIN
    -- Participants are blocked only as starting points, not as neighbours.
    seed_entity_ids := memory_core.graph_seed_entities(target_bot_id, seed_entity_ids, expected_revision);
    IF seed_topic_ids IS NULL OR EXISTS (
        SELECT 1 FROM unnest(seed_topic_ids) AS requested(id) LEFT JOIN memory_core.topics t
        ON t.id=requested.id AND t.bot_id=target_bot_id WHERE t.id IS NULL
    ) THEN RAISE EXCEPTION 'unknown or cross-space topic seed'; END IF;
    IF target_item_kind NOT IN ('atomic','topic_summary','all') OR include_archived IS NULL
       OR match_count IS NULL OR match_count<0 OR match_count>1000 THEN
        RAISE EXCEPTION 'invalid graph search scope';
    END IF;
    RETURN QUERY WITH
    seed_entities AS (
        SELECT unnest(COALESCE(seed_entity_ids, ARRAY[]::INTEGER[])) AS entity_id
    ),
    seed_topics AS (
        SELECT unnest(COALESCE(seed_topic_ids, ARRAY[]::INTEGER[])) AS topic_id
    ),
    eligible_memories AS (
        SELECT mi.id, mi.source_date, mi.created_at
        FROM memory_core.memory_items AS mi
        WHERE mi.bot_id = target_bot_id
          AND (include_archived OR mi.archive_reason IS NULL)
          AND (
              target_item_kind = 'all'
              OR mi.item_kind = target_item_kind
          )
    ),
    entity_direct_stats AS (
        SELECT
            me.entity_id,
            count(DISTINCT me.memory_id) AS linked_memory_count
        FROM memory_core.memory_entities AS me
        JOIN eligible_memories AS eligible ON eligible.id = me.memory_id
        GROUP BY me.entity_id
    ),
    source_priority_relations AS (
        SELECT
            r.id,
            r.source_entity_id,
            r.target_entity_id,
            r.relation_type,
            r.source_memory_id
        FROM memory_core.relations AS r
        WHERE r.bot_id = target_bot_id
          AND r.relation_type IN (
              'SUPPORTS',
              'CAUSES',
              'SETS_RULE',
              'VIOLATES',
              'JUDGES',
              'DESIRES',
              'RELATES_TO',
              'PROMISES',
              'LOVES',
              'CONFLICTS_WITH'
          )
    ),
    hop_priority_relations AS (
        SELECT
            r.id,
            r.source_entity_id,
            r.target_entity_id,
            r.relation_type,
            r.source_memory_id
        FROM memory_core.relations AS r
        WHERE r.bot_id = target_bot_id
          AND r.relation_type IN (
              'SUPPORTS',
              'CAUSES',
              'SETS_RULE',
              'VIOLATES',
              'JUDGES',
              'DESIRES',
              'RELATES_TO'
          )
    ),
    entity_direct_candidates AS (
        SELECT
            me.memory_id,
            least(
                1.00,
                (1.00 / NULLIF(ln(stats.linked_memory_count + 1), 0))
            )::DOUBLE PRECISION AS raw_score,
            ('实体直接关联：' || entity.name) AS graph_reason,
            'entity_direct'::TEXT AS graph_source,
            0 AS hop_count,
            entity.id AS seed_entity_id,
            NULL::INTEGER AS seed_topic_id,
            NULL::TEXT AS relation_type,
            NULL::INTEGER AS neighbor_entity_id,
            row_number() OVER (
                PARTITION BY seed.entity_id
                ORDER BY eligible.source_date DESC NULLS LAST,
                         eligible.created_at DESC NULLS LAST,
                         me.memory_id DESC
            ) AS rn
        FROM seed_entities AS seed
        JOIN memory_core.entities AS entity ON entity.id = seed.entity_id
        JOIN entity_direct_stats AS stats ON stats.entity_id = seed.entity_id
        JOIN memory_core.memory_entities AS me ON me.entity_id = seed.entity_id
        JOIN eligible_memories AS eligible ON eligible.id = me.memory_id
    ),
    entity_direct_hits AS (
        SELECT
            memory_id,
            raw_score,
            graph_reason,
            graph_source,
            hop_count,
            seed_entity_id,
            seed_topic_id,
            relation_type,
            neighbor_entity_id
        FROM entity_direct_candidates
        WHERE rn <= 5
    ),
    topic_direct_hits AS (
        SELECT
            mt.memory_id,
            0.85::DOUBLE PRECISION AS raw_score,
            ('主题直接关联：' || topic.name) AS graph_reason,
            'topic_direct'::TEXT AS graph_source,
            0 AS hop_count,
            NULL::INTEGER AS seed_entity_id,
            topic.id AS seed_topic_id,
            NULL::TEXT AS relation_type,
            NULL::INTEGER AS neighbor_entity_id
        FROM seed_topics AS seed
        JOIN memory_core.topics AS topic ON topic.id = seed.topic_id
        JOIN memory_core.memory_topics AS mt ON mt.topic_id = seed.topic_id
        JOIN eligible_memories AS eligible ON eligible.id = mt.memory_id
    ),
    relation_source_candidates AS (
        SELECT
            relation.source_memory_id AS memory_id,
            0.75::DOUBLE PRECISION AS raw_score,
            ('关系来源记忆：' || relation.relation_type) AS graph_reason,
            'relation_source_memory'::TEXT AS graph_source,
            1 AS hop_count,
            seed.entity_id AS seed_entity_id,
            NULL::INTEGER AS seed_topic_id,
            relation.relation_type,
            CASE
                WHEN relation.source_entity_id = seed.entity_id THEN relation.target_entity_id
                ELSE relation.source_entity_id
            END AS neighbor_entity_id,
            eligible.source_date,
            eligible.created_at AS memory_created_at,
            row_number() OVER (
                PARTITION BY seed.entity_id, relation.relation_type
                ORDER BY eligible.source_date DESC NULLS LAST,
                         eligible.created_at DESC NULLS LAST,
                         relation.source_memory_id DESC
            ) AS relation_rank
        FROM seed_entities AS seed
        JOIN source_priority_relations AS relation
          ON relation.source_entity_id = seed.entity_id
          OR relation.target_entity_id = seed.entity_id
        JOIN eligible_memories AS eligible ON eligible.id = relation.source_memory_id
    ),
    relation_source_per_type AS (
        SELECT *
        FROM relation_source_candidates
        WHERE relation_rank <= 5
    ),
    relation_source_dedup AS (
        SELECT DISTINCT ON (memory_id)
            memory_id,
            raw_score,
            graph_reason,
            graph_source,
            hop_count,
            seed_entity_id,
            seed_topic_id,
            relation_type,
            neighbor_entity_id,
            source_date,
            memory_created_at,
            relation_rank
        FROM relation_source_per_type
        ORDER BY memory_id,
                 relation_rank,
                 source_date DESC NULLS LAST,
                 memory_created_at DESC NULLS LAST
    ),
    relation_source_ranked AS (
        SELECT
            *,
            row_number() OVER (
                ORDER BY relation_rank,
                         source_date DESC NULLS LAST,
                         memory_created_at DESC NULLS LAST,
                         memory_id DESC
            ) AS global_rank
        FROM relation_source_dedup
    ),
    relation_source_hits AS (
        SELECT
            memory_id,
            raw_score,
            graph_reason,
            graph_source,
            hop_count,
            seed_entity_id,
            seed_topic_id,
            relation_type,
            neighbor_entity_id
        FROM relation_source_ranked
        WHERE global_rank <= 10
    ),
    entity_hop1_hits AS (
        SELECT
            me.memory_id,
            0.60::DOUBLE PRECISION AS raw_score,
            ('1-hop：' || seed_name.name || ' -> ' || relation.relation_type || ' -> ' || neighbor.name) AS graph_reason,
            'entity_hop1'::TEXT AS graph_source,
            1 AS hop_count,
            seed.entity_id AS seed_entity_id,
            NULL::INTEGER AS seed_topic_id,
            relation.relation_type,
            neighbor.id AS neighbor_entity_id
        FROM seed_entities AS seed
        JOIN hop_priority_relations AS relation
          ON relation.source_entity_id = seed.entity_id
          OR relation.target_entity_id = seed.entity_id
        JOIN memory_core.entities AS seed_name ON seed_name.id = seed.entity_id
        JOIN memory_core.entities AS neighbor
          ON neighbor.id = CASE
              WHEN relation.source_entity_id = seed.entity_id THEN relation.target_entity_id
              ELSE relation.source_entity_id
          END
        JOIN memory_core.memory_entities AS me ON me.entity_id = neighbor.id
        JOIN eligible_memories AS eligible ON eligible.id = me.memory_id
        WHERE neighbor.id <> seed.entity_id
    ),
    all_hits AS (
        SELECT * FROM entity_direct_hits
        UNION ALL
        SELECT * FROM topic_direct_hits
        UNION ALL
        SELECT * FROM relation_source_hits
        UNION ALL
        SELECT * FROM entity_hop1_hits
    ),
    dedup AS (
        SELECT
            memory_id,
            max(raw_score) AS raw_score,
            (array_agg(graph_reason ORDER BY raw_score DESC))[1] AS graph_reason,
            (array_agg(graph_source ORDER BY raw_score DESC))[1] AS graph_source,
            min(hop_count) AS hop_count,
            (array_agg(seed_entity_id ORDER BY raw_score DESC NULLS LAST))[1] AS seed_entity_id,
            (array_agg(seed_topic_id ORDER BY raw_score DESC NULLS LAST))[1] AS seed_topic_id,
            (array_agg(relation_type ORDER BY raw_score DESC NULLS LAST))[1] AS relation_type,
            (array_agg(neighbor_entity_id ORDER BY raw_score DESC NULLS LAST))[1] AS neighbor_entity_id
        FROM all_hits
        GROUP BY memory_id
    )
    SELECT
        memory_id,
        raw_score,
        graph_reason,
        graph_source,
        hop_count,
        seed_entity_id,
        seed_topic_id,
        relation_type,
        neighbor_entity_id
    FROM dedup
    ORDER BY raw_score DESC, memory_id DESC
    LIMIT greatest(0, match_count);
END
$function$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA memory_core FROM PUBLIC;
