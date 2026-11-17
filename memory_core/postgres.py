"""The first storage slice: card + keyword transactions and two retrieval paths."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os

from psycopg.types.json import Jsonb

from .clock import format_timestamp
from .config import Settings
from .db import connect
from .schema import SUPPORTED_VERSIONS


@dataclass(frozen=True)
class KeywordToken:
    term: str
    field: str = "content"
    source_id: int | None = None
    term_frequency: int = 1
    field_length: int = 1


class PostgresStore:
    def __init__(self, dsn: str, space_id: str):
        if not isinstance(space_id, str) or not space_id.strip():
            raise ValueError("必须明确指定记忆空间")
        self.dsn, self.space_id = dsn, space_id
        with connect(dsn) as connection:
            record = connection.execute("SELECT version,settings,application_role FROM memory_core.installation").fetchone()
            if not record or record["version"] not in SUPPORTED_VERSIONS:
                raise ValueError("请先安装支持的数据库版本")
            current = connection.execute("SELECT current_user AS name").fetchone()["name"]
            if current != record["application_role"]:
                raise ValueError("存储接口必须使用已登记的应用角色")
            self.settings = Settings(**record["settings"])
            self.schema_version = record["version"]

    @classmethod
    def from_env(cls, space_id: str):
        dsn = os.environ.get("MEMORY_DSN")
        if not dsn:
            raise ValueError("未设置 MEMORY_DSN")
        return cls(dsn, space_id)

    def _vector(self, values):
        if values is None or len(values) != self.settings.dimensions:
            raise ValueError("向量长度与安装维度不一致")
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in values):
            raise ValueError("向量必须只包含有限数值")
        if not any(v != 0 for v in values):
            raise ValueError("余弦检索不接受零向量")
        return "[" + ",".join(str(float(v)) for v in values) + "]"

    def _tokens(self, memory_id, tokens):
        timestamp = format_timestamp(datetime.now(timezone.utc), self.settings)
        return [dict(
            field=token.field, source_id=memory_id if token.source_id is None else token.source_id,
            term=token.term, term_frequency=token.term_frequency,
            field_length=token.field_length, updated_at=timestamp,
        ) for token in tokens]

    def write_card(
        self,
        content: str,
        embedding,
        tokens=(),
        *,
        title=None,
        summary=None,
        source_date=None,
        item_kind="atomic",
        connection=None
    ) -> int:
        vector = self._vector(embedding)
        def write(current):
            card = current.execute(
                """INSERT INTO memory_core.memory_items
                (bot_id,content,embedding,title,summary,source_date,item_kind)
                VALUES (%s,%s,%s::public.vector,%s,%s,%s,%s) RETURNING id""",
                (
                    self.space_id,
                    content,
                    vector,
                    title,
                    summary,
                    source_date,
                    item_kind,
                ),
            ).fetchone()
            current.execute(
                "SELECT memory_core.replace_memory_keyword_tokens(%s,%s,%s)",
                (card["id"], self.space_id, Jsonb(self._tokens(card["id"], tokens))),
            )
            return card["id"]

        # Trusted server-side importers may compose metadata and topic writes
        # into this same transaction rather than leave a half-imported card.
        if connection is not None:
            return write(connection)
        with connect(self.dsn) as current:
            return write(current)

    def replace_keywords(self, memory_id: int, tokens) -> int:
        with connect(self.dsn) as connection:
            result = connection.execute("SELECT memory_core.replace_memory_keyword_tokens(%s,%s,%s) AS count",
                (memory_id, self.space_id, Jsonb(self._tokens(memory_id, tokens)))).fetchone()
            return result["count"]

    @staticmethod
    def _search_options(limit, item_kind, include_archived):
        if type(limit) is not int or not 0 <= limit <= 1000:
            raise ValueError("检索数量必须为 0–1000 的整数")
        if item_kind not in ("atomic", "topic_summary", "all") or type(include_archived) is not bool:
            raise ValueError("检索范围无效")

    def search_vectors(self, embedding, *, limit=30, item_kind="atomic", include_archived=False):
        self._search_options(limit, item_kind, include_archived)
        with connect(self.dsn) as connection:
            return connection.execute("SELECT * FROM memory_core.search_vectors(%s::public.vector,%s,%s,%s,%s)",
                (self._vector(embedding), self.space_id, limit, item_kind, include_archived)).fetchall()

    def search_keywords(self, query: str, *, limit=30, item_kind="atomic", include_archived=False):
        """Query is pre-tokenized, using the existing 'term OR term' BM25 contract."""
        self._search_options(limit, item_kind, include_archived)
        with connect(self.dsn) as connection:
            return connection.execute("SELECT * FROM memory_core.search_keywords(%s,%s,%s,%s,%s)",
                (query, self.space_id, limit, item_kind, include_archived)).fetchall()
