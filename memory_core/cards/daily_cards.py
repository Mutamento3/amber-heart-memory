"""Daily conversation material using the existing draft and review workflow."""

from ..conversations.conversation_material import extraction_segments, finished_day
from ..conversations.conversations import ConversationStore
from ..daily.daily_tasks import DailyTasks, require_task
from ..storage.db import connect
from .drafts import context_digest, context_for
from .review import ReviewStore
from ..storage.schema import DAILY_EXTRACTION_VERSION
from ..storage.space import read_policy


class CardSourceChanged(ValueError):
    code = "extraction_source_changed"


class CardReviewModeChanged(ValueError):
    code = "extraction_review_mode_changed"


class DailyCardStore(ConversationStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        if self.schema_version < DAILY_EXTRACTION_VERSION:
            raise ValueError("每日抽卡需要先升级记忆核心到版本 14")

    def _source(self, c, day, now):
        source = finished_day(c, self.space_id, day, self.settings, now, "extraction")
        if source["status"] != "ready":
            return source
        segments, days, digest = extraction_segments(
            c, self.space_id, source["turns"], self.settings
        )
        return dict(
            status="ready",
            day=day,
            segments=segments,
            message_days=days,
            source_digest=digest,
            source_turn_ids=[t["id"] for t in source["turns"]],
            policy=source["policy"],
        )

    def prepare(self, task, *, now):
        if task["bot_id"] != self.space_id or task["task_kind"] != "extraction":
            raise ValueError("抽卡任务与空间不符")
        store = ReviewStore(self.dsn, self.space_id)
        tasks = DailyTasks(self.dsn, self.space_id)
        with connect(self.dsn) as c:
            read_policy(c, self.space_id)
            self._lock(c)
            previous = require_task(c, task)["result"] or {}
            source = self._source(c, task["business_date"], now)
            if source["status"] != "ready":
                return source
            digest = source["source_digest"]
            changed = previous.get("source_digest") != digest
            if changed and previous.get("extraction_id"):
                obsolete = c.execute(
                    """SELECT id,version FROM memory_core.pending_memories
                    WHERE bot_id=%s AND extraction_id=%s AND status IN ('pending','structure_pending','structure_failed')
                    ORDER BY id FOR UPDATE""",
                    (self.space_id, previous["extraction_id"]),
                ).fetchall()
                for draft in obsolete:
                    store.reject(
                        draft["id"], expected_version=draft["version"], connection=c
                    )
            # Break numeric runs in an opaque hash so the normal credential
            # screen cannot mistake a generated source key for a card number.
            key_digest = ":".join(digest[i : i + 8] for i in range(0, len(digest), 8))
            identifier = store.submit(
                "conversation:" + source["day"] + ":" + key_digest,
                "daily-card:" + source["day"] + ":" + key_digest,
                source["segments"],
                business_date=source["day"],
                message_business_dates=source["message_days"],
                connection=c,
            )
            ids = list(dict.fromkeys([*previous.get("extraction_ids", []), identifier]))
            extraction = store._extraction(c, identifier)
            unselected = (
                extraction["status"] in ("pending", "failed")
                and not c.execute(
                    "SELECT 1 FROM memory_core.pending_memories WHERE bot_id=%s AND extraction_id=%s LIMIT 1",
                    (self.space_id, identifier),
                ).fetchone()
            )
            signature = (
                context_digest(context_for(c, self.space_id))
                if changed or unselected
                else previous["context_digest"]
            )
            progress = dict(
                extraction_id=identifier,
                extraction_ids=ids,
                source_digest=digest,
                source_turn_ids=source["source_turn_ids"],
                context_digest=signature,
            )
            tasks.progress(task, progress, connection=c)
            return {**source, **progress}

    def resume(self, plan):
        store = ReviewStore(self.dsn, self.space_id)
        extraction = store.extraction(plan["extraction_id"])
        if terminal_drafts(extraction):
            return store
        if extraction["policy_revision"] != plan["policy"]["revision"]:
            store.reconfirm_extraction(
                extraction["id"],
                expected_version=extraction["version"],
                expected_policy_revision=plan["policy"]["revision"],
                unchanged_context_digest=plan["context_digest"],
            )
        return store

    def guard(self, connection, task, plan, *, now, automatic=False):
        policy = read_policy(connection, self.space_id)
        self._lock(connection)
        require_task(connection, task)
        if automatic and not policy.get("auto_approve_enabled",policy.get("review_mode")=="auto"):
            raise CardReviewModeChanged("到期自动入库已关闭")
        actual = self._source(connection, plan["day"], now)
        if (
            actual["status"] != "ready"
            or actual["source_digest"] != plan["source_digest"]
            or actual["source_turn_ids"] != plan["source_turn_ids"]
        ):
            raise CardSourceChanged("抽卡期间原话版本已改变")

    def check(self, task, plan, *, now):
        with connect(self.dsn) as c:
            self.guard(c, task, plan, now=now)

    def counts(self, extraction_ids):
        with connect(self.dsn) as c:
            return extraction_counts(c, self.space_id, extraction_ids)


def extraction_counts(connection, space, extraction_ids):
    row = connection.execute(
        """SELECT count(*) FILTER (WHERE status='approved') AS added_count,
        count(*) FILTER (WHERE status IN ('pending','structure_pending','structure_failed')) AS pending_count,
        count(*) FILTER (WHERE dedup_state='duplicate') AS duplicate_count
        FROM memory_core.pending_memories WHERE bot_id=%s AND extraction_id=ANY(%s)""",
        (space, extraction_ids),
    ).fetchone()
    return dict(row)


def terminal_drafts(extraction):
    return bool(extraction["drafts"]) and all(
        d["status"] in ("approved", "rejected", "deleted") for d in extraction["drafts"]
    )
