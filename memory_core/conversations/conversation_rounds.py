"""Transactional turn selection, tool continuations and expiring space leases."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4
from psycopg.types.json import Jsonb
from ..storage.clock import business_day_label, format_timestamp
from .conversation_context import (
    content_parts,
    freeze_context,
    continuation_body,
    tool_result_pairs,
)
from ..storage.db import connect
from ..storage.schema import HOOK_VERSION

DEFAULT_TURN_TIMEOUT_SECONDS = 300


class ConversationConflict(ValueError):
    def __init__(self, message, code="invalid_turn", record=None):
        super().__init__(message)
        self.code, self.record = code, record


class DuplicateRequest(ConversationConflict):
    pass


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def lock_space_state(c, space_id):
    """The per-space conversation lock; anything that writes receipts takes it first."""
    c.execute(
        "INSERT INTO memory_core.conversation_space_state(bot_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (space_id,),
    )
    return c.execute(
        "SELECT * FROM memory_core.conversation_space_state WHERE bot_id=%s FOR UPDATE",
        (space_id,),
    ).fetchone()


class ConversationRounds:
    def _lock(self, c):
        return lock_space_state(c, self.space_id)

    @staticmethod
    def _release(c, space):
        c.execute(
            """UPDATE memory_core.conversation_space_state SET active_turn_id=NULL,active_request_id=NULL,
            lease_token=NULL,lease_expires_at=NULL,updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s""",
            (space,),
        )

    @classmethod
    def _release_locked(cls, c, row):
        cls._release(c, row["bot_id"])
        row.update(
            active_turn_id=None, active_request_id=None, lease_token=None, lease_expires_at=None
        )

    @classmethod
    def _abandon_waiting_tools_locked(cls, c, row, now):
        if row["active_turn_id"] is None or row["active_request_id"] is not None:
            return None
        # The last upstream request already finished. Keep its result and the
        # tool ledger, but discard the context saved for a future continuation.
        abandoned = c.execute(
            """UPDATE memory_core.conversation_turns
            SET generation_state='incomplete',frozen_context=NULL,finished_at=memory_core.format_timestamp(%s)
            WHERE id=%s AND bot_id=%s AND generation_state='waiting_tools' RETURNING id""",
            (now, row["active_turn_id"], row["bot_id"]),
        ).fetchone()
        if abandoned is None:
            return None
        cls._release_locked(c, row)
        return abandoned["id"]

    @classmethod
    def _expire_locked(cls, c, row, now):
        if row["active_turn_id"] is None or datetime.fromisoformat(row["lease_expires_at"]) > now:
            return False
        c.execute(
            "UPDATE memory_core.conversation_turns SET generation_state='incomplete',frozen_context=NULL,finished_at=memory_core.format_timestamp(%s) WHERE id=%s AND bot_id=%s",
            (now, row["active_turn_id"], row["bot_id"]),
        )
        c.execute(
            "UPDATE memory_core.conversation_requests SET state='interrupted',error_stage='lease',error_code='turn_timeout',finished_at=memory_core.format_timestamp(%s) WHERE id=%s AND bot_id=%s AND state IN ('pending','requesting')",
            (now, row["active_request_id"], row["bot_id"]),
        )
        cls._release_locked(c, row)
        return True

    @classmethod
    def expire_leases(cls, dsn, *, now=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("时间必须带偏移")
        with connect(dsn) as c:
            rows = c.execute(
                "SELECT * FROM memory_core.conversation_space_state WHERE active_turn_id IS NOT NULL AND lease_expires_at::timestamptz<=%s ORDER BY bot_id FOR UPDATE SKIP LOCKED",
                (now,),
            ).fetchall()
            return sum(cls._expire_locked(c, row, now) for row in rows)

    @staticmethod
    def _owns(row, record):
        return (
            row["active_turn_id"] == record["turn_id"]
            and row["active_request_id"] == record["request_id"]
            and str(row["lease_token"]) == str(record["lease_token"])
        )

    def renew(self, record, *, now=None):
        now = now or datetime.now(timezone.utc)
        with connect(self.dsn) as c:
            row = self._lock(c)
            self._expire_locked(c, row, now)
            if not self._owns(row, record):
                return False
            c.execute(
                "UPDATE memory_core.conversation_space_state SET lease_expires_at=%s,updated_at=%s WHERE bot_id=%s",
                (
                    format_timestamp(now + timedelta(seconds=self.lease_seconds), self.settings),
                    format_timestamp(now, self.settings),
                    self.space_id,
                ),
            )
        return True

    def request_finished(self, record):
        with connect(self.dsn) as c:
            row = c.execute('SELECT state FROM memory_core.conversation_requests WHERE id=%s AND turn_id=%s AND bot_id=%s',
                (record['request_id'], record['turn_id'], self.space_id)).fetchone()
            return row is not None and row['state'] in ('complete','upstream_failed')

    def _latest(self, c):
        # The proxy's similarity heuristic must never replace an explicitly
        # numbered external turn when both clients share a space.
        proxy_only = " AND t.hook_session IS NULL" if self.schema_version >= HOOK_VERSION else ""
        return c.execute(
            "SELECT * FROM memory_core.conversation_turns t WHERE bot_id=%s AND history_state='current' AND content_deleted_at IS NULL"
            + proxy_only + " ORDER BY position_id DESC LIMIT 1",
            (self.space_id,),
        ).fetchone()

    def _same_user(self, c, turn_id, parts):
        row = c.execute(
            """SELECT content_parts FROM memory_core.conversation_messages
            WHERE turn_id=%s AND bot_id=%s AND role='user' AND event_index=0""",
            (turn_id, self.space_id),
        ).fetchone()
        return row is not None and row["content_parts"] == parts

    def _deleted_prompt(self, c, target, latest, anchor_id, anchor_kind, parts):
        if target is not None:
            return c.execute(
                """SELECT 1 FROM memory_core.conversation_turns
                WHERE bot_id=%s AND id=%s AND context_manifest ? 'deleted_fingerprints'""",
                (self.space_id, target),
            ).fetchone() is not None
        # An exact retry of a surviving turn still takes precedence.
        if (latest and latest["anchor_message_id"] == anchor_id and latest["anchor_kind"] == anchor_kind
                and self._same_user(c, latest["id"], parts)):
            return False
        proxy_only = " AND t.hook_session IS NULL" if self.schema_version >= HOOK_VERSION else ""
        return c.execute(
            """SELECT 1 FROM memory_core.conversation_turns t
            JOIN memory_core.conversation_messages m ON (m.turn_id,m.bot_id)=(t.id,t.bot_id)
            WHERE t.bot_id=%s AND t.content_deleted_at IS NOT NULL AND m.role='user' AND m.event_index=0
            AND t.context_manifest->'deleted_fingerprints'->>m.id::text=%s
            AND t.anchor_message_id IS NOT DISTINCT FROM %s AND t.anchor_kind=%s"""
            + proxy_only + " LIMIT 1",
            (self.space_id, fingerprint(parts), anchor_id, anchor_kind),
        ).fetchone() is not None

    def _classify(self, c, body, intent, user_index, latest):
        anchor_id, anchor_kind = None, "absent"
        prefix = body["messages"][:user_index]
        proxy_only = " AND t.hook_session IS NULL" if self.schema_version >= HOOK_VERSION else ""
        if prefix and prefix[-1]["role"] == "assistant":
            digest = fingerprint(content_parts(prefix[-1].get("content")))
            previous_user = next((m for m in reversed(prefix) if m["role"] == "user"), None)
            user_digest = fingerprint(content_parts(previous_user.get("content"))) if previous_user else None
            # A removed reply can still delimit a client request. Prefer the
            # matching user/reply pair when several rounds said the same thing.
            anchor = c.execute(
                """SELECT m.id FROM memory_core.conversation_messages m
                JOIN memory_core.conversation_turns t ON (t.id,t.bot_id)=(m.turn_id,m.bot_id)
                LEFT JOIN memory_core.conversation_messages u ON (u.turn_id,u.bot_id)=(t.id,t.bot_id)
                    AND u.role='user' AND u.event_index=0
                WHERE m.bot_id=%s AND m.role='assistant'
                AND ((m.match_fingerprint=%s AND t.history_state='current' AND t.content_deleted_at IS NULL)
                    OR (t.content_deleted_at IS NOT NULL AND t.context_manifest->'deleted_fingerprints'->>m.id::text=%s))"""
                + proxy_only + """
                ORDER BY (COALESCE(u.match_fingerprint,t.context_manifest->'deleted_fingerprints'->>u.id::text)=%s)
                    DESC NULLS LAST, t.position_id DESC,t.id DESC,m.event_index DESC LIMIT 1""",
                (self.space_id, digest, digest, user_digest),
            ).fetchone()
            anchor_id = anchor["id"] if anchor else None
            anchor_kind = "matched" if anchor else "unmatched"
        elif any(m["role"] == "assistant" for m in prefix):
            anchor_kind = "unmatched"
        operation, target = intent.get("operation"), intent.get("target_turn_id")
        parts = content_parts(body["messages"][user_index].get("content"))
        if operation != "new" and self._deleted_prompt(c, target, latest, anchor_id, anchor_kind, parts):
            raise ValueError("这轮聊天已删除，请发送一条新消息")
        if operation:
            if operation == "new":
                if target is not None:
                    raise ConversationConflict("新消息不能指定替换目标")
                return "new", None, anchor_id, anchor_kind
            if latest is None:
                raise ConversationConflict("首发只支持重做最新采用的轮次")
            source = latest
            if operation == "retry" and target not in (None, latest["id"]):
                source = c.execute(
                    "SELECT * FROM memory_core.conversation_turns WHERE id=%s AND bot_id=%s AND history_state='candidate' AND replaces_turn_id=%s AND content_deleted_at IS NULL",
                    (target, self.space_id, latest["id"]),
                ).fetchone()
                if source is None:
                    raise ConversationConflict("只能重试最新轮次或其失败候选")
            elif target not in (None, latest["id"]):
                raise ConversationConflict("首发只支持重做最新采用的轮次")
            if operation == "retry" and source["generation_state"] not in ("failed", "incomplete"):
                raise ConversationConflict("这一轮已完成；需要新回复请使用重新生成")
            if operation in ("retry", "regenerate"):
                if not self._same_user(c, source["id"], parts):
                    raise ConversationConflict("重新生成或重试须使用原用户消息，改写请使用 edit")
            return operation, latest, source["anchor_message_id"], source["anchor_kind"]
        if target is not None:
            raise ConversationConflict("指定目标轮次时须明确操作类型")
        if latest:
            newest = c.execute(
                """SELECT m.id,m.turn_id FROM memory_core.conversation_messages m JOIN memory_core.conversation_turns t ON (t.id,t.bot_id)=(m.turn_id,m.bot_id)
                WHERE m.bot_id=%s AND m.role='assistant' AND t.history_state='current' AND t.content_deleted_at IS NULL
                """ + proxy_only + " ORDER BY t.position_id DESC,m.event_index DESC LIMIT 1",
                (self.space_id,),
            ).fetchone()
            if newest is None or newest["turn_id"] != latest["id"]:
                # Without a reply, the newest AI message is still the previous
                # turn's anchor. Retaining the unanswered user in the request
                # distinguishes a followup from redoing that same position.
                last_assistant = next(
                    (
                        i for i in range(len(prefix) - 1, -1, -1)
                        if prefix[i]["role"] == "assistant"
                    ),
                    -1,
                )
                if any(m["role"] == "user" for m in prefix[last_assistant + 1:]):
                    return "new", None, anchor_id, anchor_kind
                if anchor_id is not None and anchor_id == latest["anchor_message_id"]:
                    operation = "regenerate" if self._same_user(c, latest["id"], parts) else "edit"
                    return operation, latest, anchor_id, anchor_kind
            if anchor_id is not None and newest and anchor_id == newest["id"]:
                return "new", None, anchor_id, anchor_kind
            if anchor_id is not None and anchor_id == latest["anchor_message_id"]:
                return "edit", latest, anchor_id, anchor_kind
            if anchor_kind == "absent":
                if (
                    parts
                    and all(p["type"] == "text" for p in parts)
                    and self._same_user(c, latest["id"], parts)
                ):
                    return "regenerate", latest, None, "absent"
        return "new", None, anchor_id, anchor_kind

    def _request(self, c, body, intent, turn_id, step, operation, digest, timestamp, retry_of=None):
        row = c.execute(
            """INSERT INTO memory_core.conversation_requests
            (bot_id,turn_id,step_index,client_request_id,client_operation,target_turn_id,resolved_operation,retry_of_request_id,request_fingerprint,model,stream,state,started_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'requesting',%s) RETURNING id""",
            (
                self.space_id,
                turn_id,
                step,
                intent.get("request_id"),
                intent.get("operation"),
                intent.get("target_turn_id"),
                operation,
                retry_of,
                digest,
                body["model"],
                body.get("stream", False),
                timestamp,
            ),
        ).fetchone()
        return {"turn_id": turn_id, "request_id": row["id"], "step_index": step}

    def _claim(self, c, record, now, timestamp):
        c.execute(
            """UPDATE memory_core.conversation_space_state SET active_turn_id=%s,active_request_id=%s,
            lease_token=%s,lease_expires_at=%s,updated_at=%s WHERE bot_id=%s""",
            (
                record["turn_id"],
                record["request_id"],
                record["lease_token"],
                format_timestamp(now + timedelta(seconds=self.lease_seconds), self.settings),
                timestamp,
                self.space_id,
            ),
        )

    def begin(self, body, intent, *, now=None):
        now = now or datetime.now(timezone.utc)
        timestamp = format_timestamp(now, self.settings)
        digest = fingerprint({"body": body, "intent": intent})
        user_index = next(
            (
                i
                for i in range(len(body["messages"]) - 1, -1, -1)
                if body["messages"][i]["role"] == "user"
            ),
            None,
        )
        with connect(self.dsn) as c:
            if not c.execute(
                "SELECT 1 FROM memory_core.space_policies WHERE bot_id=%s AND user_entity_id IS NOT NULL",
                (self.space_id,),
            ).fetchone():
                raise ValueError("空间不存在或尚未初始化")
            row = self._lock(c)
            self._expire_locked(c, row, now)
            if intent.get("request_id"):
                prior = c.execute(
                    "SELECT id AS request_id,turn_id,state,request_fingerprint FROM memory_core.conversation_requests WHERE bot_id=%s AND client_request_id=%s",
                    (self.space_id, intent["request_id"]),
                ).fetchone()
                if prior:
                    code = (
                        "duplicate_request"
                        if prior.pop("request_fingerprint") == digest
                        else "request_id_conflict"
                    )
                    raise DuplicateRequest("请求编号已使用，不会再次调用上游", code, prior)
            last_role = body["messages"][-1]["role"]
            if last_role == "tool":
                return self._continue(c, body, intent, user_index, row, now, timestamp, digest)
            if row["active_turn_id"] is not None and (
                last_role != "user" or self._abandon_waiting_tools_locked(c, row, now) is None
            ):
                raise ConversationConflict("此空间正在处理，请稍后再发", "space_busy")
            if user_index is None or user_index != len(body["messages"]) - 1:
                raise ValueError("新一轮请求必须以用户消息结尾")
            operation, replaced, anchor, anchor_kind = self._classify(
                c, body, intent, user_index, self._latest(c)
            )
            if (
                replaced
                and self.schema_version >= 12
                and c.execute(
                    """SELECT 1 FROM memory_core.conversation_summary_turns st
                JOIN memory_core.conversation_summaries s ON (s.id,s.bot_id)=(st.summary_id,st.bot_id)
                WHERE st.bot_id=%s AND st.turn_id=%s AND s.content_deleted_at IS NULL""",
                    (self.space_id, replaced["id"]),
                ).fetchone()
            ):
                raise ConversationConflict(
                    "这一轮已被摘要覆盖，不能替换，请作为新消息发送", "summary_covered"
                )
            parts = content_parts(body["messages"][user_index].get("content"))
            turn_id = c.execute(
                "SELECT nextval(pg_get_serial_sequence('memory_core.conversation_turns','id')) AS id"
            ).fetchone()["id"]
            # Frontend history is used only to recognize the turn. Never retain
            # it in a hosted snapshot or send it as the historical context.
            initial = dict(body, messages=[m for m in body["messages"][:user_index]
                if m["role"] in ("system", "developer")] + [body["messages"][user_index]])
            frozen = freeze_context(initial, len(initial["messages"]) - 1, timestamp)
            c.execute(
                """INSERT INTO memory_core.conversation_turns(id,bot_id,position_id,replaces_turn_id,anchor_message_id,anchor_kind,
                generation_state,history_state,business_date,created_at,history_changed_at,frozen_context,context_manifest)
                VALUES (%s,%s,%s,%s,%s,%s,'generating',%s,%s,%s,%s,%s,%s)""",
                (
                    turn_id,
                    self.space_id,
                    replaced["position_id"] if replaced else turn_id,
                    replaced["id"] if replaced else None,
                    anchor,
                    anchor_kind,
                    "candidate" if replaced else "current",
                    business_day_label(now, self.settings).isoformat(),
                    timestamp,
                    timestamp,
                    Jsonb(frozen),
                    Jsonb(
                        {
                            "mode": "preparing",
                            "schema_version": 1,
                            "frozen_at": timestamp,
                        }
                    ),
                ),
            )
            retry_of = None
            if operation == "retry":
                retry_of = c.execute(
                    "SELECT id FROM memory_core.conversation_requests WHERE turn_id=%s AND bot_id=%s ORDER BY step_index DESC LIMIT 1",
                    (intent.get("target_turn_id") or replaced["id"], self.space_id),
                ).fetchone()["id"]
            record = self._request(
                c, body, intent, turn_id, 0, operation, digest, timestamp, retry_of
            )
            c.execute(
                """INSERT INTO memory_core.conversation_messages(bot_id,turn_id,request_id,event_index,role,content_parts,match_fingerprint,content_state,occurred_at)
                VALUES (%s,%s,%s,0,'user',%s,%s,'complete',%s)""",
                (
                    self.space_id,
                    turn_id,
                    record["request_id"],
                    Jsonb(parts),
                    fingerprint(parts),
                    timestamp,
                ),
            )
            if replaced is None:
                c.execute(
                    "UPDATE memory_core.conversation_space_state SET history_revision=history_revision+1 WHERE bot_id=%s",
                    (self.space_id,),
                )
            record["lease_token"] = str(uuid4())
            self._claim(c, record, now, timestamp)
            record["upstream_body"] = initial
        return record

    def _ledger(self, c, turn_id):
        rows = c.execute(
            """SELECT event_index,'message' AS kind,content_parts AS payload,NULL AS tool_call_id FROM memory_core.conversation_messages
            WHERE turn_id=%s AND bot_id=%s AND role='assistant' UNION ALL
            SELECT event_index,kind,payload,tool_call_id FROM memory_core.conversation_tool_events WHERE turn_id=%s AND bot_id=%s ORDER BY event_index""",
            (turn_id, self.space_id, turn_id, self.space_id),
        ).fetchall()
        messages = []
        for row in rows:
            if row["kind"] == "message":
                messages.append({"role": "assistant", "content": row["payload"]})
            elif row["kind"] == "call":
                if not messages or messages[-1]["role"] != "assistant":
                    messages.append({"role": "assistant", "content": None})
                messages[-1].setdefault("tool_calls", []).append(
                    {"id": row["tool_call_id"], "type": "function", "function": row["payload"]}
                )
            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": row["tool_call_id"],
                        "content": row["payload"]["content"],
                    }
                )
        return messages

    def _continue(self, c, body, intent, user_index, row, now, timestamp, digest):
        if row["active_turn_id"] is None:
            raise ConversationConflict("没有等待工具的轮次，可能已结束或超时", "no_tool_round")
        if row["active_request_id"] is not None:
            raise ConversationConflict("此空间正在处理，请稍后再发", "space_busy")
        turn_id = row["active_turn_id"]
        if intent.get("target_turn_id") not in (None, turn_id):
            raise ConversationConflict("工具续轮目标与当前轮次不符")
        turn = c.execute(
            "SELECT * FROM memory_core.conversation_turns WHERE id=%s AND bot_id=%s",
            (turn_id, self.space_id),
        ).fetchone()
        if turn["generation_state"] != "waiting_tools" or not turn["frozen_context"]:
            raise ConversationConflict("这一轮没有等待工具", "no_tool_round")
        ledger = self._ledger(c, turn_id)
        pending = ledger[-1].get("tool_calls", []) if ledger else []
        try:
            pairs = tool_result_pairs(body["messages"], pending)
        except ValueError as exc:
            raise ConversationConflict(
                str(exc), "tool_results_mismatch"
            ) from None
        results = [dict(message, tool_call_id=original_id) for original_id, message in pairs]
        expected = ledger + results
        upstream = continuation_body(body, turn["frozen_context"], user_index, expected, self.settings)
        step = c.execute(
            "SELECT max(step_index)+1 AS step FROM memory_core.conversation_requests WHERE turn_id=%s AND bot_id=%s",
            (turn_id, self.space_id),
        ).fetchone()["step"]
        record = self._request(c, body, intent, turn_id, step, "tool_continue", digest, timestamp)
        index = self._next_event(c, turn_id)
        for message in results:
            c.execute(
                """INSERT INTO memory_core.conversation_tool_events(bot_id,turn_id,request_id,event_index,kind,tool_call_id,payload)
                VALUES (%s,%s,%s,%s,'result',%s,%s)""",
                (
                    self.space_id,
                    turn_id,
                    record["request_id"],
                    index,
                    message["tool_call_id"],
                    Jsonb({"content": content_parts(message.get("content"))}),
                ),
            )
            index += 1
        c.execute(
            "UPDATE memory_core.conversation_turns SET generation_state='generating' WHERE id=%s AND bot_id=%s",
            (turn_id, self.space_id),
        )
        record["lease_token"] = str(row["lease_token"])
        self._claim(c, record, now, timestamp)
        record["upstream_body"] = upstream
        return record

    def _next_event(self, c, turn_id):
        return c.execute(
            """SELECT coalesce(max(event_index),0)+1 AS next FROM
            (SELECT event_index FROM memory_core.conversation_messages WHERE turn_id=%s AND bot_id=%s UNION ALL
             SELECT event_index FROM memory_core.conversation_tool_events WHERE turn_id=%s AND bot_id=%s) events""",
            (turn_id, self.space_id, turn_id, self.space_id),
        ).fetchone()["next"]

    def finish(
        self,
        record,
        *,
        content="",
        tools=(),
        state="complete",
        finish_reason=None,
        usage=None,
        response_id=None,
        error_stage=None,
        error_code=None,
        visible_content=None,
        reported=(),
        reported_none=False,
        summary_trigger_count=None,
        now=None
    ):
        now = now or datetime.now(timezone.utc)
        timestamp = format_timestamp(now, self.settings)
        with connect(self.dsn) as c:
            space = self._lock(c)
            self._expire_locked(c, space, now)
            if not self._owns(space, record):
                return False
            turn = c.execute(
                "SELECT * FROM memory_core.conversation_turns WHERE id=%s AND bot_id=%s FOR UPDATE",
                (record["turn_id"], self.space_id),
            ).fetchone()
            if turn["content_deleted_at"] is not None:
                content, tools = "", ()
                state, error_stage, error_code = "archive_failed", "archive", "source_expired"
            if state == "complete" and (
                (finish_reason == "tool_calls" and not tools)
                or (tools and finish_reason != "tool_calls")
            ):
                state, error_stage, error_code = "interrupted", "upstream", "invalid_tool_calls"
            waiting = state == "complete" and finish_reason == "tool_calls"
            successful = (
                state == "complete" and not waiting and finish_reason == "stop" and bool(content)
            )
            generation = (
                "waiting_tools"
                if waiting
                else (
                    "complete"
                    if successful
                    else "failed" if state == "upstream_failed" else "incomplete"
                )
            )
            index = self._next_event(c, turn["id"])
            if content and state != "upstream_failed":
                parts = content_parts(content)
                # Matching may use a separately filtered downstream body in 3C.
                match = content_parts(content if visible_content is None else visible_content)
                c.execute(
                    """INSERT INTO memory_core.conversation_messages(bot_id,turn_id,request_id,event_index,role,content_parts,match_fingerprint,content_state,occurred_at)
                    VALUES (%s,%s,%s,%s,'assistant',%s,%s,%s,%s)""",
                    (
                        self.space_id,
                        turn["id"],
                        record["request_id"],
                        index,
                        Jsonb(parts),
                        fingerprint(match),
                        (
                            "complete"
                            if state == "complete" and finish_reason != "length"
                            else "incomplete"
                        ),
                        timestamp,
                    ),
                )
                index += 1
            if state != "upstream_failed":
                for tool in tools:
                    c.execute(
                        """INSERT INTO memory_core.conversation_tool_events(bot_id,turn_id,request_id,event_index,kind,tool_call_id,tool_name,payload)
                        VALUES (%s,%s,%s,%s,'call',%s,%s,%s)""",
                        (
                            self.space_id,
                            turn["id"],
                            record["request_id"],
                            index,
                            tool["id"],
                            tool["function"]["name"],
                            Jsonb(tool["function"]),
                        ),
                    )
                    index += 1
            c.execute(
                """UPDATE memory_core.conversation_requests SET state=%s,finish_reason=%s,usage=%s,upstream_response_id=%s,
                error_stage=%s,error_code=%s,finished_at=%s WHERE id=%s AND bot_id=%s""",
                (
                    state,
                    finish_reason,
                    Jsonb(usage) if usage is not None else None,
                    response_id,
                    error_stage,
                    error_code,
                    timestamp,
                    record["request_id"],
                    self.space_id,
                ),
            )
            if state not in ("upstream_failed", "archive_failed"):
                self._save_receipt(c, turn, record, reported, reported_none, timestamp)
            if successful and turn["history_state"] == "candidate":
                old = c.execute(
                    "UPDATE memory_core.conversation_turns SET history_state='replaced',history_changed_at=%s WHERE id=%s AND bot_id=%s AND history_state='current' RETURNING id",
                    (timestamp, turn["replaces_turn_id"], self.space_id),
                ).fetchone()
                if not old:
                    raise ConversationConflict("替换目标已改变，不能采用过期候选")
                c.execute(
                    "UPDATE memory_core.conversation_turns SET history_state='current',history_changed_at=%s WHERE id=%s AND bot_id=%s",
                    (timestamp, turn["id"], self.space_id),
                )
                c.execute(
                    "UPDATE memory_core.conversation_space_state SET history_revision=history_revision+1 WHERE bot_id=%s",
                    (self.space_id,),
                )
            c.execute(
                """UPDATE memory_core.conversation_turns SET generation_state=%s,finished_at=%s,
                frozen_context=CASE WHEN %s THEN frozen_context ELSE NULL END WHERE id=%s AND bot_id=%s""",
                (generation, None if waiting else timestamp, waiting, turn["id"], self.space_id),
            )
            if waiting:
                c.execute(
                    "UPDATE memory_core.conversation_space_state SET active_request_id=NULL,lease_expires_at=%s,updated_at=%s WHERE bot_id=%s",
                    (
                        format_timestamp(
                            now + timedelta(seconds=self.lease_seconds), self.settings
                        ),
                        timestamp,
                        self.space_id,
                    ),
                )
            else:
                from .conversation_material import save_summary_progress

                save_summary_progress(
                    c, self.space_id, turn["id"], turn["business_date"],
                    summary_trigger_count, self.schema_version,
                )
                self._release(c, self.space_id)
        return True
