"""Pair external turns without taking a hosted generation lease."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from ..storage.clock import business_day_label, format_timestamp
from ..storage.db import connect
from ..storage.space import read_policy
from .conversation_context import content_parts
from .conversation_history import memory_context_sections
from .conversation_rounds import ConversationConflict, fingerprint, lock_space_state
from .conversations import ConversationStore


PENDING_LIFETIME = timedelta(minutes=30)
PENDING_FILTER = """hook_session IS NOT NULL AND generation_state='pending'
    AND content_deleted_at IS NULL AND created_at::timestamptz <= %s"""


def _expired(turn, now):
    return turn["generation_state"] == "pending" and (
        datetime.fromisoformat(turn["created_at"]) + PENDING_LIFETIME <= now
    )


class HookTurns(ConversationStore):
    def session_context(self, recent_diary_count, *, now=None):
        now = now or datetime.now(timezone.utc)
        day = business_day_label(now, self.settings).isoformat()
        calendar_day = now.astimezone(ZoneInfo(self.settings.timezone)).date()
        with connect(self.dsn) as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            policy = read_policy(c, self.space_id)
            source = self._memory_context(c, policy, day, calendar_day, recent_diary_count)
        return memory_context_sections(dict(source, policy=policy), day, self.settings)

    def _find(self, c, session, prompt):
        # For older Claude Code builds, only the latest unkeyed prompt can
        # match a Stop. Expired rows still shadow older keyless prompts.
        prompt_filter = "hook_prompt IS NULL"
        args = (self.space_id, session)
        if prompt is not None:
            prompt_filter = "hook_prompt=%s AND content_deleted_at IS NULL"
            args += (prompt,)
        return c.execute(
            """SELECT * FROM memory_core.conversation_turns
            WHERE bot_id=%s AND hook_session=%s AND history_state <> 'replaced' AND """
            + prompt_filter + " ORDER BY position_id DESC LIMIT 1",
            args,
        ).fetchone()

    def reserve(self, session, prompt, content, *, now=None):
        now = now or datetime.now(timezone.utc)
        timestamp = format_timestamp(now, self.settings)
        # Image data exists only in this request and in the bounded caption job.
        parts, digest = content_parts(content), fingerprint(content)
        with connect(self.dsn) as c:
            self._lock(c)
            turn = self._find(c, session, prompt) if prompt is not None else None
            if turn:
                if _expired(turn, now):
                    self._discard(c, self.space_id, [turn["id"]], timestamp)
                else:
                    request = c.execute(
                        """SELECT id,request_fingerprint FROM memory_core.conversation_requests
                        WHERE bot_id=%s AND turn_id=%s AND step_index=0""",
                        (self.space_id, turn["id"]),
                    ).fetchone()
                    if request["request_fingerprint"] != digest:
                        raise ConversationConflict("这轮已关联另一条用户消息", "hook_prompt_conflict")
                    return dict(turn_id=turn["id"], request_id=request["id"], step_index=0,
                                recall=turn["recall_snapshot"], created=False)
            turn_id = self._new_turn(c, session, prompt, timestamp, business_day_label(now, self.settings).isoformat())
            record = self._request(c, {"model": "hook"}, {}, turn_id, 0, "new", digest, timestamp)
            self._message(c, record, "user", parts, timestamp)
        return dict(record, recall={"status": "not_run", "cards": []}, created=True)

    def _new_turn(self, c, session, prompt, timestamp, day, previous=None):
        turn_id = c.execute(
            "SELECT nextval(pg_get_serial_sequence('memory_core.conversation_turns','id')) AS id"
        ).fetchone()["id"]
        c.execute(
            """INSERT INTO memory_core.conversation_turns
            (id,bot_id,position_id,replaces_turn_id,history_state,business_date,
             created_at,history_changed_at,hook_session,hook_prompt,context_manifest,recall_snapshot)
            VALUES (%s,%s,%s,%s,'candidate',%s,%s,%s,%s,%s,%s,%s)""",
            (turn_id, self.space_id, previous["position_id"] if previous else turn_id,
             previous["id"] if previous else None, day,
             previous["created_at"] if previous else timestamp, timestamp, session, prompt,
             Jsonb(previous["context_manifest"] if previous else dict(mode="hook")),
             Jsonb(previous["recall_snapshot"] if previous else dict(status="not_run", cards=[]))),
        )
        return turn_id

    def _message(self, c, record, role, parts, timestamp, images=None):
        c.execute(
            """INSERT INTO memory_core.conversation_messages
            (bot_id,turn_id,request_id,event_index,role,content_parts,
             match_fingerprint,content_state,occurred_at,image_descriptions)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'complete',%s,%s)""",
            (self.space_id, record["turn_id"], record["request_id"], int(role == "assistant"),
             role, Jsonb(parts), fingerprint(parts), timestamp,
             Jsonb(images) if images is not None else None),
        )

    def save_recall(self, turn_id, snapshot):
        with connect(self.dsn) as c:
            self._lock(c)
            turn = c.execute(
                """SELECT * FROM memory_core.conversation_turns
                WHERE bot_id=%s AND id=%s AND content_deleted_at IS NULL""",
                (self.space_id, turn_id),
            ).fetchone()
            if not turn or _expired(turn, datetime.now(timezone.utc)):
                raise ConversationConflict("暂存轮次已过期", "hook_turn_missing")
            if turn["recall_snapshot"]["status"] != "not_run":
                return turn["recall_snapshot"]
            c.execute(
                """UPDATE memory_core.conversation_turns SET recall_snapshot=%s,context_manifest=%s
                WHERE bot_id=%s AND id=%s""",
                (Jsonb(snapshot), Jsonb(dict(mode="hook", recall_ids=[
                    card["memory_id"] for card in snapshot["cards"]
                ])), self.space_id, turn_id),
            )
        return snapshot

    def complete(self, session, prompt, reply, *, now=None):
        now = now or datetime.now(timezone.utc)
        timestamp = format_timestamp(now, self.settings)
        parts = content_parts(reply)
        with connect(self.dsn) as c:
            self._lock(c)
            turn = self._find(c, session, prompt)
            if not turn or turn["content_deleted_at"] is not None or _expired(turn, now):
                raise ConversationConflict("找不到可配对的轮次", "hook_turn_missing")
            if turn["recall_snapshot"]["status"] == "not_run":
                raise ConversationConflict("本轮召回尚未完成，请稍后重试", "hook_recall_pending")
            rows = c.execute(
                """SELECT m.*,r.request_fingerprint FROM memory_core.conversation_messages m
                JOIN memory_core.conversation_requests r ON (r.id,r.bot_id)=(m.request_id,m.bot_id)
                WHERE m.bot_id=%s AND m.turn_id=%s ORDER BY m.event_index""",
                (self.space_id, turn["id"]),
            ).fetchall()
            user = rows[0]
            replaced = turn["generation_state"] == "complete"
            if replaced and rows[1]["content_parts"] == parts:
                return dict(turn_id=turn["id"], replaced=False)
            turn_id = turn["id"]
            record = dict(turn_id=turn_id, request_id=user["request_id"])
            day = turn["business_date"]
            if replaced:
                c.execute(
                    """UPDATE memory_core.conversation_turns SET history_state='replaced',history_changed_at=%s
                    WHERE id=%s AND bot_id=%s""",
                    (timestamp, turn_id, self.space_id),
                )
                self._invalidate_summary(c, turn_id, timestamp)
                turn_id = self._new_turn(c, session, prompt, timestamp, day, previous=turn)
                record = self._request(c, {"model": "hook"}, {}, turn_id, 0,
                                       "regenerate", user["request_fingerprint"], timestamp)
                self._message(c, record, "user", user["content_parts"], user["occurred_at"], user["image_descriptions"])
            else:
                # Publish to the day of the first completed pair so an in-flight
                # hook across the business-day boundary cannot miss a closed job.
                day = business_day_label(now, self.settings).isoformat()
            self._message(c, record, "assistant", parts, timestamp)
            c.execute(
                """UPDATE memory_core.conversation_requests SET state='complete',finish_reason='stop',finished_at=%s
                WHERE bot_id=%s AND id=%s""",
                (timestamp, self.space_id, record["request_id"]),
            )
            self._save_receipt(c, turn, record, (), False, timestamp)
            c.execute(
                """UPDATE memory_core.conversation_turns
                SET generation_state='complete',history_state='current',finished_at=%s,
                    history_changed_at=%s,business_date=%s
                WHERE bot_id=%s AND id=%s""",
                (timestamp, timestamp, day, self.space_id, turn_id),
            )
            c.execute(
                "UPDATE memory_core.conversation_space_state SET history_revision=history_revision+1 WHERE bot_id=%s",
                (self.space_id,),
            )
        return dict(turn_id=turn_id, replaced=replaced)

    def _invalidate_summary(self, c, turn_id, timestamp):
        # Affected leaves and rollups must stay out of future summary batches.
        c.execute(
            """UPDATE memory_core.conversation_summaries s SET content=NULL,content_deleted_at=%s
            FROM memory_core.conversation_summary_turns st
            WHERE st.bot_id=%s AND st.turn_id=%s AND s.bot_id=st.bot_id
            AND s.content_deleted_at IS NULL
            AND st.summary_id IN (s.id,s.first_summary_id,s.second_summary_id,s.third_summary_id)""",
            (timestamp, self.space_id, turn_id),
        )

    @staticmethod
    def _discard(c, space, ids, timestamp):
        if not ids:
            return
        c.execute(
            """UPDATE memory_core.conversation_messages SET content_parts=NULL,image_descriptions=NULL,
            match_fingerprint=NULL,content_deleted_at=%s WHERE bot_id=%s AND turn_id=ANY(%s)""",
            (timestamp, space, ids),
        )
        c.execute(
            """UPDATE memory_core.conversation_requests SET state='interrupted',error_stage='hook',
            error_code='hook_turn_expired',finished_at=%s WHERE bot_id=%s AND turn_id=ANY(%s)""",
            (timestamp, space, ids),
        )
        c.execute(
            """UPDATE memory_core.conversation_turns SET generation_state='incomplete',finished_at=%s,
            content_deleted_at=%s,context_manifest='{}',recall_snapshot='{"status":"expired","cards":[]}'
            WHERE bot_id=%s AND id=ANY(%s)""",
            (timestamp, timestamp, space, ids),
        )

    @classmethod
    def expire_pending(cls, dsn, settings, now):
        cutoff = now - PENDING_LIFETIME
        stamp = format_timestamp(now, settings)
        with connect(dsn) as c:
            spaces = c.execute(
                "SELECT DISTINCT bot_id FROM memory_core.conversation_turns WHERE " + PENDING_FILTER,
                (cutoff,),
            ).fetchall()
        for space in spaces:
            with connect(dsn) as c:
                lock_space_state(c, space["bot_id"])
                turns = c.execute(
                    "SELECT id FROM memory_core.conversation_turns WHERE bot_id=%s AND " + PENDING_FILTER,
                    (space["bot_id"], cutoff),
                ).fetchall()
                cls._discard(c, space["bot_id"], [turn["id"] for turn in turns], stamp)
