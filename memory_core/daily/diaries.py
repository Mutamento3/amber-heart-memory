"""Daily diaries from exact adopted body versions, with guarded user editing."""

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from ..conversations import chat_text as text
from ..storage.clock import format_timestamp
from ..conversations.conversation_material import dialogue_input, finished_day
from ..conversations.conversations import ConversationStore
from .daily_tasks import finish_task, require_task
from ..storage.db import connect
from ..storage.schema import DIARY_VERSION
from ..storage.space import read_policy


class DiaryConflict(ValueError):
    pass


class DiarySourceChanged(ValueError):
    code = "diary_source_changed"


def recent_diaries(connection, space, day, count, *, calendar_day):
    cutoff = (calendar_day - timedelta(days=count)).isoformat()
    rows = connection.execute(
        """SELECT id,business_date,content,revision FROM memory_core.conversation_diaries
        WHERE bot_id=%s AND business_date>=%s AND business_date<%s ORDER BY business_date DESC LIMIT %s""",
        (space, cutoff, day, count),
    ).fetchall()
    return list(reversed(rows))


class DiaryStore(ConversationStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        if self.schema_version < DIARY_VERSION:
            raise ValueError("日记需要先升级记忆核心到版本 13")

    def get(self, day):
        with connect(self.dsn) as c:
            return c.execute(
                "SELECT * FROM memory_core.conversation_diaries WHERE bot_id=%s AND business_date=%s",
                (self.space_id, day),
            ).fetchone()

    def _source(self, c, day, now):
        source = finished_day(c, self.space_id, day, self.settings, now, "diary")
        if source["status"] != "ready":
            return source
        policy, turns = source["policy"], source["turns"]
        dialogue, digest, _ = dialogue_input(
            c, self.space_id, turns, policy, self.settings
        )
        revision = c.execute(
            "SELECT history_revision FROM memory_core.conversation_space_state WHERE bot_id=%s",
            (self.space_id,),
        ).fetchone()
        return dict(
            status="ready",
            day=day,
            source_ids=[t["id"] for t in turns],
            source_digest=digest,
            source_revision=revision["history_revision"],
            prompt=text.DIARY_PROMPT.format(
                user_name=policy["participants"]["user"]["name"],
                assistant_name=policy["participants"]["assistant"]["name"],
            ),
            material=text.DIARY_MATERIAL.format(
                date=day, persona=policy["persona"], dialogue=dialogue,
                user_name=policy["participants"]["user"]["name"],
                core_memory=policy.get("core_memory", ""),
            ),
        )

    def plan(self, day, *, now):
        with connect(self.dsn) as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return self._source(c, day, now)

    def publish(self, plan, result, task, *, now):
        content, model = result.get("content"), result.get("model")
        if (
            not isinstance(content, str)
            or not content.strip()
            or not isinstance(model, str)
            or not 1 <= len(model) <= 256
        ):
            raise ValueError("日记正文或型号无效")
        if (
            task["bot_id"] != self.space_id
            or task["task_kind"] != "diary"
            or task["business_date"] != plan["day"]
        ):
            raise ValueError("日记与任务的空间或日期不符")
        timestamp = format_timestamp(now, self.settings)
        with connect(self.dsn) as c:
            # Match chat's policy -> space lock order, then fence this attempt.
            read_policy(c, self.space_id)
            self._lock(c)
            require_task(c, task)
            existing = c.execute(
                "SELECT id FROM memory_core.conversation_diaries WHERE bot_id=%s AND business_date=%s",
                (self.space_id, plan["day"]),
            ).fetchone()
            if existing:
                finish_task(
                    c,
                    task,
                    timestamp,
                    result=dict(diary_id=existing["id"], existing=True),
                )
                return existing["id"]
            current = self._source(c, plan["day"], now)
            if (
                current["status"] != "ready"
                or current["source_digest"] != plan["source_digest"]
                or current["source_ids"] != plan["source_ids"]
            ):
                raise DiarySourceChanged("日记来源已经变化，请重新生成")
            row = c.execute(
                """INSERT INTO memory_core.conversation_diaries
                (bot_id,business_date,content,source_revision,source_digest,source_turn_ids,model,usage,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    self.space_id,
                    plan["day"],
                    content.strip(),
                    plan["source_revision"],
                    plan["source_digest"],
                    plan["source_ids"],
                    model,
                    Jsonb(result["usage"]) if result.get("usage") is not None else None,
                    timestamp,
                    timestamp,
                ),
            ).fetchone()
            finish_task(c, task, timestamp, result=dict(diary_id=row["id"]))
            return row["id"]

    def edit(self, identifier, content, expected_revision, *, now=None):
        if (
            type(identifier) is not int
            or identifier <= 0
            or type(expected_revision) is not int
            or expected_revision <= 0
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 100000
        ):
            raise ValueError("日记正文或修订号无效")
        timestamp = format_timestamp(now or datetime.now(timezone.utc), self.settings)
        with connect(self.dsn) as c:
            row = c.execute(
                """UPDATE memory_core.conversation_diaries SET content=%s,revision=revision+1,
                user_edited=true,edited_at=%s,updated_at=%s WHERE id=%s AND bot_id=%s AND revision=%s RETURNING *""",
                (
                    content.strip(),
                    timestamp,
                    timestamp,
                    identifier,
                    self.space_id,
                    expected_revision,
                ),
            ).fetchone()
            if not row:
                raise DiaryConflict("日记已在别处修改或不存在，请核对最新版本后保存")
            return row
