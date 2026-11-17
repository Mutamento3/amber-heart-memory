"""Read-only, explicit-space queries for three-route recall and existing parents."""

from .db import connect
from .graph_store import GraphStore
from .schema import RECALL_VERSION
from .space import RevisionConflict, read_policy


class SearchStore(GraphStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        with connect(dsn) as connection:
            if connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"] < RECALL_VERSION:
                raise ValueError("请先升级到三路检索支持的数据库版本")

    def check_revision(self, revision):
        if self.policy()["revision"] != revision:
            raise RevisionConflict("检索期间配置已改变，请重试")

    def entities(self, ids=None):
        with connect(self.dsn) as connection:
            if ids is not None:
                return connection.execute("SELECT id,name,aliases FROM memory_core.entities WHERE bot_id=%s AND id=ANY(%s) ORDER BY id",
                                          (self.space_id, list(ids))).fetchall()
            # Direct Postgres has no PostgREST row cap; fetch the complete catalog.
            return connection.execute("SELECT id,name,aliases FROM memory_core.entities WHERE bot_id=%s ORDER BY id",
                                      (self.space_id,)).fetchall()

    def search_entities(self, embedding, *, limit=10):
        self._search_options(limit, "all", False)
        with connect(self.dsn) as connection:
            return connection.execute("SELECT * FROM memory_core.search_entities(%s::public.vector,%s,%s)",
                                      (self._vector(embedding), self.space_id, limit)).fetchall()

    def name_evidence(self, rows, terms):
        pairs = {(row["memory_id"], row["matched_entity_id"]) for row in rows if row.get("matched_entity_id")}
        if not pairs or not terms:
            return {}
        with connect(self.dsn) as connection:
            hits = connection.execute("""SELECT memory_id,source_id,field,term FROM memory_core.memory_keyword_tokens
                WHERE bot_id=%s AND memory_id=ANY(%s) AND source_id=ANY(%s)
                AND field IN ('entity_name','alias') AND term=ANY(%s)
                ORDER BY memory_id,source_id,field,term""", (self.space_id,
                    sorted({mid for mid, _ in pairs}), sorted({eid for _, eid in pairs}), terms)).fetchall()
        evidence = {}
        for hit in hits:
            pair = (hit["memory_id"], hit["source_id"])
            if pair in pairs:
                evidence.setdefault(pair, []).append({"field": hit["field"], "term": hit["term"]})
        return evidence

    def search_graph(self, entity_ids, topic_ids, *, revision, limit=30, item_kind="atomic", include_archived=False):
        self._search_options(limit, item_kind, include_archived)
        for values in (entity_ids, topic_ids):
            if not isinstance(values, (list, tuple)) or any(type(n) is not int or n <= 0 for n in values):
                raise ValueError("图起点必须为正整数编号")
        with connect(self.dsn) as connection:
            policy = read_policy(connection, self.space_id)
            if policy["revision"] != revision:
                raise RevisionConflict("图检索配置已改变")
            return connection.execute("SELECT * FROM memory_core.search_graph(%s,%s,%s,%s,%s,%s,%s)",
                (list(entity_ids), list(topic_ids), self.space_id, limit, item_kind, include_archived, revision)).fetchall()

    def cards(self, ids):
        if not ids:
            return {}
        with connect(self.dsn) as connection:
            rows = connection.execute("""SELECT id,bot_id,item_kind,title,content,summary,source_date,
                source_kind,experience_mode,temperature,parent_reflection_id,archive_reason
                FROM memory_core.memory_items WHERE bot_id=%s AND id=ANY(%s)""",
                (self.space_id, list(ids))).fetchall()
        return {row["id"]: row for row in rows}

    def source_references(self, ids):
        """Return provenance references and short evidence, never raw material."""
        if not ids:
            return {}
        with connect(self.dsn) as connection:
            rows = connection.execute("""SELECT p.memory_item_id,p.id AS draft_id,m.id AS material_id,
                m.source_key,m.deleted_at,p.payload->'evidence' AS evidence
                FROM memory_core.pending_memories p
                JOIN memory_core.material_extractions e ON e.id=p.extraction_id AND e.bot_id=p.bot_id
                JOIN memory_core.source_materials m ON m.id=e.material_id AND m.bot_id=e.bot_id
                WHERE p.bot_id=%s AND p.status='approved' AND p.memory_item_id=ANY(%s)""",
                (self.space_id, list(ids))).fetchall()
        return {r['memory_item_id']:dict(memory_id=r['memory_item_id'],draft_id=r['draft_id'],
            material_id=r['material_id'],source_key=r['source_key'],evidence=r['evidence'],
            source_status='deleted' if r['deleted_at'] else 'available') for r in rows}
