"""Conversation store: lifecycle and retention without HTTP dependencies."""

from .conversation_rounds import (
    ConversationRounds,
    ConversationConflict,
    DuplicateRequest,
    fingerprint,
    DEFAULT_TURN_TIMEOUT_SECONDS,
)
from .conversation_context import content_parts
from ..storage.clock import format_timestamp
from ..storage.db import connect
from ..storage.postgres import PostgresStore
from ..storage.schema import CONVERSATION_VERSION, IMAGE_DESCRIPTION_VERSION, SUMMARY_VERSION
from .conversation_history import ConversationHistory

# Instant summaries only serve the current day (later days are covered by diaries),
# so they are kept for 15 days even when raw conversations are kept longer.
SUMMARY_RETENTION_DAYS = 15


def invalidate_summaries(c, space, ids, timestamp, *, retention_now=None):
    age = ""
    params = [space, ids]
    if retention_now is not None:
        age = """OR s.start_at::timestamptz<=%s::timestamptz-make_interval(days=>%s)
            OR (p.conversation_retention_days IS NOT NULL
                AND s.start_at::timestamptz<=%s::timestamptz-make_interval(days=>p.conversation_retention_days))"""
        params.extend([retention_now, SUMMARY_RETENTION_DAYS, retention_now])
    summaries = c.execute(
        """WITH affected AS (
            SELECT s.id FROM memory_core.conversation_summaries s
            JOIN memory_core.space_policies p USING(bot_id)
            WHERE s.bot_id=%s AND s.content_deleted_at IS NULL
            AND (EXISTS (SELECT 1 FROM memory_core.conversation_summary_turns st
                WHERE st.summary_id=s.id AND st.bot_id=s.bot_id AND st.turn_id=ANY(%s))
                """ + age + """)
        )
        UPDATE memory_core.conversation_summaries s SET content=NULL,content_deleted_at=%s
        WHERE s.bot_id=%s AND s.content_deleted_at IS NULL
        AND EXISTS (SELECT 1 FROM affected a
            WHERE a.id IN (s.id,s.first_summary_id,s.second_summary_id,s.third_summary_id))
        RETURNING s.id,s.business_date""",
        (*params, timestamp, space),
    ).fetchall()
    if summaries:
        # Intact leaves can be merged again when their old rollup is gone.
        c.execute(
            """UPDATE memory_core.conversation_summaries SET merged_into_id=NULL
            WHERE bot_id=%s AND content_deleted_at IS NULL AND merged_into_id=ANY(%s)""",
            (space, [summary["id"] for summary in summaries]),
        )
    return summaries


def clear_turn_contents(c, space, ids, timestamp, *, schema_version, manual=False):
    """Clear the stored bodies and extraction copies inside the space lock."""
    if not ids:
        return
    from .conversation_material import segment_id

    messages = c.execute(
        "SELECT id FROM memory_core.conversation_messages WHERE bot_id=%s AND turn_id=ANY(%s)",
        (space, ids),
    ).fetchall()
    c.execute(
        """UPDATE memory_core.source_materials s SET segments=NULL,deleted_at=%s
        WHERE s.bot_id=%s AND s.deleted_at IS NULL AND EXISTS
        (SELECT 1 FROM jsonb_array_elements(s.segments) e WHERE e->>'segment_id'=ANY(%s))""",
        (timestamp, space, [segment_id(m["id"]) for m in messages]),
    )
    # Manual deletions keep only the matching digests needed to recognize a
    # client's stale history. Retention still erases the ordinary digests.
    manifest = """jsonb_build_object('deleted_fingerprints', COALESCE((
        SELECT jsonb_object_agg(m.id::text,m.match_fingerprint)
        FROM memory_core.conversation_messages m WHERE m.bot_id=t.bot_id AND m.turn_id=t.id
        AND m.match_fingerprint IS NOT NULL), '{}'::jsonb))""" if manual else "'{}'::jsonb"
    c.execute(
        """UPDATE memory_core.conversation_turns t SET frozen_context=NULL,
        recall_snapshot='{"status":"expired","cards":[]}',content_deleted_at=%s,context_manifest="""
        + manifest + " WHERE t.bot_id=%s AND t.id=ANY(%s)",
        (timestamp, space, ids),
    )
    image_cleanup = "image_descriptions=NULL," if schema_version >= IMAGE_DESCRIPTION_VERSION else ""
    c.execute(
        "UPDATE memory_core.conversation_messages SET content_parts=NULL," + image_cleanup +
        " match_fingerprint=NULL,content_deleted_at=%s WHERE bot_id=%s AND turn_id=ANY(%s)",
        (timestamp, space, ids),
    )
    c.execute(
        """UPDATE memory_core.conversation_tool_events SET payload=NULL,content_deleted_at=%s
        WHERE bot_id=%s AND turn_id=ANY(%s)""",
        (timestamp, space, ids),
    )


class ConversationStore(ConversationHistory, ConversationRounds, PostgresStore):
    def __init__(self, dsn, space_id, *, lease_seconds=DEFAULT_TURN_TIMEOUT_SECONDS):
        super().__init__(dsn, space_id)
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("轮次超时秒数必须是正整数")
        self.lease_seconds = lease_seconds
        with connect(dsn) as c:
            self.schema_version = c.execute(
                "SELECT version FROM memory_core.installation"
            ).fetchone()["version"]
            if self.schema_version < CONVERSATION_VERSION:
                raise ValueError("请先升级记忆核心到版本 10")

    def delete_positions(self, positions, *, now):
        """Delete all versions at the selected positions in one transaction."""
        if not positions or any(type(pos) is not int or not 0 < pos < 2**63 for pos in positions):
            raise ValueError("聊天轮次编号无效")
        selected = set(positions)
        timestamp = format_timestamp(now, self.settings)
        with connect(self.dsn) as c:
            state = self._lock(c)
            self._expire_locked(c, state, now)
            turns = c.execute(
                """SELECT id,position_id,generation_state,content_deleted_at
                FROM memory_core.conversation_turns WHERE bot_id=%s AND position_id=ANY(%s)""",
                (self.space_id, list(selected)),
            ).fetchall()
            if {turn["position_id"] for turn in turns} != selected:
                raise ConversationConflict("所选轮次不存在或不属于当前空间", "turn_missing")
            live = [turn for turn in turns if turn["content_deleted_at"] is None]
            if any(turn["generation_state"] in ("pending", "generating", "waiting_tools") for turn in live):
                raise ConversationConflict("所选轮次正在回复或等待工具，请等这一轮结束后再删除", "turn_in_progress")
            if not live:
                return 0, []
            ids = {turn["id"] for turn in live}
            summaries = (
                invalidate_summaries(c, self.space_id, list(ids), timestamp)
                if self.schema_version >= SUMMARY_VERSION else []
            )
            if state["active_turn_id"] is not None:
                manifest = c.execute(
                    "SELECT context_manifest FROM memory_core.conversation_turns WHERE bot_id=%s AND id=%s",
                    (self.space_id, state["active_turn_id"]),
                ).fetchone()["context_manifest"]
                # A frozen prefix can be replayed by a tool continuation.
                if (manifest.get("mode") == "preparing"
                        or ids.intersection(manifest.get("history_turn_ids", []))
                        or any(s["id"] in manifest.get("summary_ids", []) for s in summaries)):
                    raise ConversationConflict("当前回复正在使用所选聊天，请等这一轮结束后再删除", "turn_in_use")
            clear_turn_contents(c, self.space_id, list(ids), timestamp, schema_version=self.schema_version, manual=True)
            c.execute(
                "UPDATE memory_core.conversation_space_state SET history_revision=history_revision+1 WHERE bot_id=%s",
                (self.space_id,),
            )
        return len({turn["position_id"] for turn in live}), sorted({summary["business_date"] for summary in summaries})

    def expire(self, *, now):
        """Idempotent retention cleanup, also used by the public daily scheduler."""
        timestamp = format_timestamp(now, self.settings)
        with connect(self.dsn) as connection:
            space_state = self._lock(connection)
            turns = connection.execute(
                """SELECT t.id FROM memory_core.conversation_turns t
                JOIN memory_core.space_policies p USING(bot_id)
                WHERE t.bot_id=%s AND p.conversation_retention_days IS NOT NULL
                AND t.content_deleted_at IS NULL
                AND t.created_at::timestamptz<=%s::timestamptz-make_interval(days=>p.conversation_retention_days)
                FOR UPDATE OF t""",
                (self.space_id, now),
            ).fetchall()
            ids = [t["id"] for t in turns]
            if self.schema_version >= SUMMARY_VERSION:
                invalidate_summaries(connection, self.space_id, ids, timestamp, retention_now=now)
            if space_state["active_turn_id"] in ids:
                connection.execute(
                    "UPDATE memory_core.conversation_turns SET generation_state='incomplete',finished_at=%s WHERE id=%s AND bot_id=%s",
                    (timestamp, space_state["active_turn_id"], self.space_id),
                )
                connection.execute(
                    """UPDATE memory_core.conversation_requests SET state='archive_failed',
                    error_stage='archive',error_code='source_expired',finished_at=%s
                    WHERE id=%s AND bot_id=%s AND state IN ('pending','requesting')""",
                    (timestamp, space_state["active_request_id"], self.space_id),
                )
                self._release(connection, self.space_id)
            clear_turn_contents(connection, self.space_id, ids, timestamp, schema_version=self.schema_version)
        return len(ids)
