"""Space-scoped graph storage and rule reads; no extraction or graph traversal."""

from uuid import uuid4

from .db import connect
from .postgres import PostgresStore
from .schema import GRAPH_VERSION
from .space import read_policy, read_topics


class AmbiguousEntity(ValueError):
    pass


class GraphStore(PostgresStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        with connect(dsn) as connection:
            if connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"] < GRAPH_VERSION:
                raise ValueError("请先升级到图谱存储支持的版本")

    def policy(self):
        with connect(self.dsn) as connection:
            return read_policy(connection, self.space_id)

    def resolve_entity(self, name):
        with connect(self.dsn) as connection:
            rows = connection.execute("""SELECT id FROM memory_core.entities WHERE bot_id=%s
                AND lookup_keys @> ARRAY[memory_core.normalize_entity_name(%s)] ORDER BY id""", (self.space_id, name)).fetchall()
            if len(rows) > 1:
                raise AmbiguousEntity("同名或别名命中多个实体，需要明确选择")
            return rows[0]["id"] if rows else None

    def create_entity(self, name, *, entity_type="PERSON", aliases=(), description=None,
                      embedding=None, entity_key=None):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("实体名不能为空")
        if not isinstance(aliases, (tuple, list)) or any(not isinstance(a, str) or not a.strip() for a in aliases):
            raise ValueError("实体别名必须是非空字符串列表")
        key = str(uuid4()) if entity_key is None else entity_key
        if not isinstance(key, str) or not key.strip() or key.startswith("participant:"):
            raise ValueError("实体键无效或属于参与者配置")
        vector = self._vector(embedding) if embedding is not None else None
        values = (self.space_id, key, name.strip(), entity_type, list(aliases), description, vector)
        with connect(self.dsn) as connection:
            read_policy(connection, self.space_id)
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("review:" + self.space_id,))
            row = connection.execute("""INSERT INTO memory_core.entities
                (bot_id,entity_key,name,entity_type,aliases,description,embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s::public.vector)
                ON CONFLICT(bot_id,entity_key) DO NOTHING RETURNING id""", values).fetchone()
            if row:
                return row["id"]
            row = connection.execute("""SELECT id FROM memory_core.entities WHERE bot_id=%s AND entity_key=%s
                AND ROW(name,entity_type,aliases,description,embedding) IS NOT DISTINCT FROM ROW(%s,%s,%s,%s,%s::public.vector)""", values).fetchone()
            if not row:
                raise ValueError("同一个实体键已用于不同内容")
            return row["id"]

    def filter_graph_seeds(self, candidates, *, expected_revision=None):
        if not isinstance(candidates, (list, tuple)) or any(type(n) is not int or n <= 0 for n in candidates):
            raise ValueError("起点必须是正整数实体编号列表")
        with connect(self.dsn) as connection:
            policy = read_policy(connection, self.space_id)
            revision = policy["revision"] if expected_revision is None else expected_revision
            if type(revision) is not int or revision <= 0:
                raise ValueError("配置修订号无效")
            return connection.execute("SELECT memory_core.graph_seed_entities(%s,%s,%s) AS ids", (self.space_id, list(candidates), revision)).fetchone()["ids"]

    def clean_address_terms(self, query):
        if not isinstance(query, str):
            raise ValueError("查询必须是字符串")
        from .keyword_index import clean_address_terms
        return clean_address_terms(query,self.policy()["ignored_terms"])

    def topics(self):
        with connect(self.dsn) as connection:
            return read_topics(connection, self.space_id)

    def topic_is_eligible(self, topic_id):
        with connect(self.dsn) as connection:
            row = connection.execute("SELECT summary_eligible FROM memory_core.topics WHERE bot_id=%s AND id=%s", (self.space_id, topic_id)).fetchone()
            if not row:
                raise ValueError("主题不存在或属于其他空间")
            return row["summary_eligible"]

    def fallback_topic(self):
        with connect(self.dsn) as connection:
            row = connection.execute("SELECT * FROM memory_core.topics WHERE bot_id=%s AND is_fallback", (self.space_id,)).fetchone()
            if not row:
                raise ValueError("空间缺少兜底主题")
            return row

    def link_entity(self, memory_id, entity_id, *, mention_role=None):
        with connect(self.dsn) as connection:
            return connection.execute("""INSERT INTO memory_core.memory_entities(bot_id,memory_id,entity_id,mention_role)
                VALUES (%s,%s,%s,%s) ON CONFLICT(bot_id,memory_id,entity_id)
                DO UPDATE SET mention_role=coalesce(excluded.mention_role,memory_core.memory_entities.mention_role)
                RETURNING id""", (self.space_id, memory_id, entity_id, mention_role)).fetchone()["id"]

    def link_topic(self, memory_id, topic_id, *, primary=False):
        if type(primary) is not bool:
            raise ValueError("主主题标记必须为布尔值")
        with connect(self.dsn) as connection:
            row = connection.execute("""INSERT INTO memory_core.memory_topics(bot_id,memory_id,topic_id)
                VALUES (%s,%s,%s) ON CONFLICT(bot_id,memory_id,topic_id)
                DO UPDATE SET topic_id=excluded.topic_id RETURNING id""", (self.space_id, memory_id, topic_id)).fetchone()
            if primary:
                connection.execute("UPDATE memory_core.memory_items SET primary_topic_id=%s WHERE id=%s AND bot_id=%s", (topic_id, memory_id, self.space_id))
            return row["id"]

    def create_relation(self, source_entity_id, target_entity_id, relation_type, *, source_memory_id=None,
                        relation_key=None, relation_type_raw=None, description=None, valid_from=None):
        key = str(uuid4()) if relation_key is None else relation_key
        if not isinstance(key, str) or not key.strip():
            raise ValueError("关系键不能为空")
        values = (self.space_id, key, source_entity_id, target_entity_id, relation_type,
                  source_memory_id, relation_type_raw, description, valid_from)
        with connect(self.dsn) as connection:
            row = connection.execute("""INSERT INTO memory_core.relations
                (bot_id,relation_key,source_entity_id,target_entity_id,relation_type,source_memory_id,relation_type_raw,description,valid_from)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(bot_id,relation_key) DO NOTHING RETURNING id""", values).fetchone()
            if row:
                return row["id"]
            row = connection.execute("""SELECT id FROM memory_core.relations WHERE bot_id=%s AND relation_key=%s
                AND ROW(source_entity_id,target_entity_id,relation_type,source_memory_id,relation_type_raw,description,valid_from)
                IS NOT DISTINCT FROM ROW(%s,%s,%s,%s,%s,%s,%s)""", values).fetchone()
            if not row:
                raise ValueError("同一个关系键已用于不同内容")
            return row["id"]
