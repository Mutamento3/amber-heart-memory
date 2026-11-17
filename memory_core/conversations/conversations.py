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
from ..storage.schema import CONVERSATION_VERSION, IMAGE_DESCRIPTION_VERSION
from .conversation_history import ConversationHistory

# Instant summaries only serve the current day (later days are covered by diaries),
# so they are kept for 15 days even when raw conversations are kept longer.
SUMMARY_RETENTION_DAYS = 15


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

    def expire(self, *, now):
        """Idempotent retention cleanup, also used by the public daily scheduler.

        Extractors use segment_id=conversation-message:<id>. Copied source
        material is expired in the same transaction, while cards keep provenance.
        """

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
            if self.schema_version >= 12:
                leaves = connection.execute(
                    """SELECT s.id FROM memory_core.conversation_summaries s
                    JOIN memory_core.space_policies p USING(bot_id) WHERE s.bot_id=%s
                    AND s.content_deleted_at IS NULL
                    AND (s.start_at::timestamptz<=%s::timestamptz-make_interval(days=>%s)
                        OR (p.conversation_retention_days IS NOT NULL
                            AND s.start_at::timestamptz<=%s::timestamptz-make_interval(days=>p.conversation_retention_days))
                        OR EXISTS (SELECT 1 FROM memory_core.conversation_summary_turns st
                            WHERE st.summary_id=s.id AND st.bot_id=s.bot_id AND st.turn_id=ANY(%s)))""",
                    (self.space_id, now, SUMMARY_RETENTION_DAYS, now, ids),
                ).fetchall()
                expired_summaries = [s["id"] for s in leaves]
                if expired_summaries:
                    connection.execute(
                        """UPDATE memory_core.conversation_summaries SET content=NULL,content_deleted_at=%s
                        WHERE bot_id=%s AND (id=ANY(%s) OR first_summary_id=ANY(%s)
                            OR second_summary_id=ANY(%s) OR third_summary_id=ANY(%s))""",
                        (
                            timestamp,
                            self.space_id,
                            expired_summaries,
                            expired_summaries,
                            expired_summaries,
                            expired_summaries,
                        ),
                    )
            if not ids:
                return 0
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
            messages = connection.execute(
                "SELECT id FROM memory_core.conversation_messages WHERE bot_id=%s AND turn_id=ANY(%s)",
                (self.space_id, ids),
            ).fetchall()
            from .conversation_material import segment_id

            segment_ids = [segment_id(m["id"]) for m in messages]
            connection.execute(
                """UPDATE memory_core.source_materials s SET segments=NULL,deleted_at=%s
                WHERE s.bot_id=%s AND s.deleted_at IS NULL AND EXISTS
                (SELECT 1 FROM jsonb_array_elements(s.segments) e WHERE e->>'segment_id'=ANY(%s))""",
                (timestamp, self.space_id, segment_ids),
            )
            image_cleanup = 'image_descriptions=NULL,' if self.schema_version >= IMAGE_DESCRIPTION_VERSION else ''
            connection.execute(
                "UPDATE memory_core.conversation_messages SET content_parts=NULL," + image_cleanup +
                " match_fingerprint=NULL,content_deleted_at=%s WHERE bot_id=%s AND turn_id=ANY(%s)",
                (timestamp, self.space_id, ids),
            )
            connection.execute(
                """UPDATE memory_core.conversation_tool_events SET payload=NULL,
                content_deleted_at=%s WHERE bot_id=%s AND turn_id=ANY(%s)""",
                (timestamp, self.space_id, ids),
            )
            connection.execute(
                """UPDATE memory_core.conversation_turns SET frozen_context=NULL,
                context_manifest='{}',recall_snapshot='{"status":"expired","cards":[]}',content_deleted_at=%s
                WHERE bot_id=%s AND id=ANY(%s)""",
                (timestamp, self.space_id, ids),
            )
        return len(ids)
