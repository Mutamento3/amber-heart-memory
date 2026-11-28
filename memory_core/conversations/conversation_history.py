"""Read adopted history and persist an owned, image-free hosted context."""

from copy import deepcopy
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from . import chat_text as text
from ..storage.clock import business_day_bounds
from .conversation_context import freeze_message, prepend_text, thaw_message, timed_content
from .conversation_rounds import ConversationConflict
from ..storage.db import connect
from ..storage.space import read_policy
from ..storage.schema import DIARY_VERSION, RECENT_NOTES_VERSION, SUMMARY_VERSION
from ..bookmarks.recent_note_text import enabled_sections, render_recent_notes, SECTIONS
from ..recall.tool_history import render_tool_card


def parts_text(parts, image_descriptions=None):
    rendered=[]
    image_index=0
    for part in parts or []:
        if part['type']=='text':
            rendered.append(part.get('text',''))
        else:
            description = image_descriptions[image_index]['description'] if image_descriptions else None
            rendered.append(text.IMAGE_DESCRIPTION.format(description=description) if description else text.IMAGE_PLACEHOLDER)
            image_index+=1
    return ''.join(rendered)


def _tool_note(tools):
    if not tools:
        return ""
    returned = {event["tool_call_id"]: event for event in tools if event["kind"] == "result"}
    notes, calls = [], []
    for event in tools:
        if event["kind"] != "call":
            continue
        card = render_tool_card(event, returned.get(event["tool_call_id"]))
        if card is None:
            status = "returned" if event["tool_call_id"] in returned else "pending"
            calls.append(text.TOOL_ITEM.format(
                name=event.get("tool_name") or text.UNNAMED_TOOL, status=text.TOOL_STATUSES[status]
            ))
        else:
            if calls:
                notes.append(text.TOOLS.format(tools=text.TOOL_SEPARATOR.join(calls)))
                calls = []
            notes.append(card)
    if calls:
        notes.append(text.TOOLS.format(tools=text.TOOL_SEPARATOR.join(calls)))
    return "\n".join(notes)


def render_turn(turn, messages, tools, settings):
    return _render_turn(turn, messages, settings, _tool_note(tools))


def _render_turn(turn, messages, settings, note=""):
    rendered = []
    has_reply = any(
        m["role"] == "assistant" and parts_text(m["content_parts"]).strip()
        for m in messages
    )
    for message in messages:
        content = parts_text(message["content_parts"],message.get("image_descriptions"))
        if message["role"] == "user":
            content = timed_content(content, message["occurred_at"], settings)
            if not has_reply:
                content += "\n" + text.UNANSWERED
        else:
            if not content.strip():
                continue
            if message["content_state"] == "incomplete":
                content += "\n" + text.INCOMPLETE
        rendered.append({"role": message["role"], "content": content})
    if note:
        if rendered and rendered[-1]["role"] == "assistant":
            rendered[-1]["content"] += "\n" + note
        else:
            rendered.append({"role": "assistant", "content": note})
    if (
        has_reply
        and turn["generation_state"] in ("incomplete", "failed")
        and not any(text.INCOMPLETE in m["content"] for m in rendered)
    ):
        # A complete tool preamble followed by a timeout is still an unfinished round.
        if rendered:
            rendered[-1]["content"] += "\n" + text.INCOMPLETE
    return rendered


def _tool_record_prefix(notes):
    return text.HISTORY_TOOL_RECORD.format(records="\n".join(notes)) if notes else ""


def _chat_history(groups):
    """Move records only in the model's dialogue, after whole-round trimming."""
    history, pending = [], []
    has_tool_records = False
    for group in groups:
        for message in group.get("chat_messages", group["messages"]):
            if pending and message["role"] == "user":
                message = dict(message, content=_tool_record_prefix(pending) + message["content"])
                pending.clear()
            history.append(message)
        if note := group.get("tool_note"):
            has_tool_records = True
            pending.append(note)
    return history, _tool_record_prefix(pending), has_tool_records


def memory_context_sections(source, day, settings):
    """The four model-visible memory sections shared by both chat entry points."""
    policy = source["policy"]
    user_name = policy["participants"]["user"]["name"]
    core = policy.get("core_memory", "")
    diaries, notes, summaries = (
        source.get("diaries", []), source.get("recent_notes", {}), source.get("summaries", [])
    )
    start, end = business_day_bounds(date.fromisoformat(day), settings)
    return {
        "core_memory": text.CORE_MEMORY_INJECTION.format(user_name=user_name, content=core)
        if core.strip() else "",
        "diary": text.DIARY_INJECTION.format(
            user_name=user_name,
            diaries="\n\n".join(
                text.DIARY_ENTRY.format(date=d["business_date"], content=d["content"]) for d in diaries
            ),
        ) if diaries else "",
        "recent_notes": render_recent_notes(notes, user_name)
        if any(notes.get(section, "").strip() for section in SECTIONS) else "",
        "summary": text.SUMMARY_INJECTION.format(
            start=f"{start.month}月{start.day}日 {start:%H:%M}",
            end=f"{end.month}月{end.day}日 {end:%H:%M}",
            summaries="\n\n".join(s["content"] for s in summaries),
        ) if summaries else "",
    }


class ConversationHistory:

    def _memory_context(self, c, policy, day, calendar_day, recent_diary_count):
        diaries = []
        recent_notes = {}
        if self.schema_version >= RECENT_NOTES_VERSION:
            notes = c.execute('SELECT period,health,longterm,daily FROM memory_core.recent_notes WHERE bot_id=%s', (self.space_id,)).fetchone()
            recent_notes = {key: notes[key] for key in enabled_sections(policy)} if notes else {}
        if self.schema_version >= DIARY_VERSION and recent_diary_count:
            from ..daily.diaries import recent_diaries

            diaries = recent_diaries(
                c, self.space_id, day, recent_diary_count,
                calendar_day=calendar_day,
            )
        summaries = []
        if self.schema_version >= SUMMARY_VERSION:
            from ..daily.summaries import active_summaries

            summaries = active_summaries(c, self.space_id, day)
        return dict(diaries=diaries, recent_notes=recent_notes, summaries=summaries)

    def history_input(
        self,
        record,
        *,
        previous_messages=4,
        max_history_chars=None,
        recent_diary_count=3,
        hook_session=None,
    ):
        """No model calls or long transactions while holding the space lock."""
        with connect(self.dsn) as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            policy = read_policy(c, self.space_id)
            turn = c.execute(
                "SELECT * FROM memory_core.conversation_turns WHERE bot_id=%s AND id=%s",
                (self.space_id, record["turn_id"]),
            ).fetchone()
            memory = self._memory_context(
                c, policy, turn["business_date"],
                datetime.fromisoformat(turn["created_at"]).astimezone(
                    ZoneInfo(self.settings.timezone)
                ).date(),
                recent_diary_count,
            )
            covered_ids = {
                identifier for s in memory["summaries"] for identifier in s["covered_turn_ids"]
            }
            session_filter = " AND hook_session=%s" if hook_session is not None else ""
            session_args = (hook_session,) if hook_session is not None else ()
            current = c.execute(
                """SELECT * FROM memory_core.conversation_turns WHERE bot_id=%s
                AND business_date=%s AND history_state='current' AND content_deleted_at IS NULL
                """ + session_filter + " ORDER BY position_id",
                (self.space_id, turn["business_date"], *session_args),
            ).fetchall()
            # The first request of a business day chooses the carryover. Later
            # requests replay that same prefix, rather than removing it at turn 2.
            seeded = next(
                (t for t in current if t["context_manifest"].get("mode") == "hosted"),
                None,
            )
            carry_ids = (
                seeded["context_manifest"].get("carryover_turn_ids", [])
                if seeded and not memory["summaries"]
                else []
            )
            current = [
                t
                for t in current
                if t["id"] != turn["id"]
                and t["position_id"] < turn["position_id"]
                and t["id"] not in covered_ids
            ]
            if not seeded and not current and previous_messages and not memory["summaries"]:
                previous = (
                    date.fromisoformat(turn["business_date"]) - timedelta(days=1)
                ).isoformat()
                candidates = c.execute(
                    """SELECT t.*,count(m.id) AS message_count FROM memory_core.conversation_turns t
                    JOIN memory_core.conversation_messages m ON (m.turn_id,m.bot_id)=(t.id,t.bot_id)
                    WHERE t.bot_id=%s AND t.business_date=%s AND t.history_state='current' AND t.content_deleted_at IS NULL
                    """ + session_filter + " GROUP BY t.id ORDER BY t.position_id DESC",
                    (self.space_id, previous, *session_args),
                ).fetchall()
                count = 0
                for old in candidates:
                    carry_ids.insert(0, old["id"])
                    count += old["message_count"]
                    if count >= previous_messages:
                        break
            carry = (
                c.execute(
                    """SELECT * FROM memory_core.conversation_turns WHERE bot_id=%s AND id=ANY(%s)
                AND history_state='current' AND content_deleted_at IS NULL ORDER BY position_id""",
                    (self.space_id, carry_ids),
                ).fetchall()
                if carry_ids
                else []
            )
            turns = carry + current
            ids = [t["id"] for t in turns]
            messages = (
                c.execute(
                    """SELECT * FROM memory_core.conversation_messages WHERE bot_id=%s
                AND turn_id=ANY(%s) AND content_deleted_at IS NULL ORDER BY turn_id,event_index""",
                    (self.space_id, ids),
                ).fetchall()
                if ids
                else []
            )
            tools = (
                c.execute(
                    """SELECT turn_id,kind,tool_call_id,tool_name,reported_status,payload FROM memory_core.conversation_tool_events
                WHERE bot_id=%s AND turn_id=ANY(%s) AND content_deleted_at IS NULL ORDER BY turn_id,event_index""",
                    (self.space_id, ids),
                ).fetchall()
                if ids
                else []
            )
        groups = []
        messages_by_turn, tools_by_turn = defaultdict(list), defaultdict(list)
        for message in messages:
            messages_by_turn[message["turn_id"]].append(message)
        for tool in tools:
            tools_by_turn[tool["turn_id"]].append(tool)
        for old in turns:
            old_messages = messages_by_turn[old["id"]]
            note = _tool_note(tools_by_turn[old["id"]])
            # Keep the existing input for recall and whole-round history budgeting.
            group = dict(turn_id=old["id"], messages=_render_turn(old, old_messages, self.settings, note))
            if note:
                group.update(tool_note=note, chat_messages=_render_turn(old, old_messages, self.settings))
            groups.append(group)
        omitted = []
        if max_history_chars is not None:
            from ..daily.summary_policy import protected_tail

            groups, omitted = protected_tail(groups, max_history_chars)
        history = [m for g in groups for m in g["messages"]]
        included = {g["turn_id"] for g in groups}
        return dict(
            policy=policy,
            turn=turn,
            history=history,
            history_groups=groups,
            carryover_turn_ids=[t["id"] for t in carry],
            history_turn_ids=[g["turn_id"] for g in groups],
            history_message_ids=[m["id"] for m in messages if m["turn_id"] in included],
            **memory,
            history_omitted=dict(
                turn_ids=[g["turn_id"] for g in omitted],
                turns=len(omitted),
                messages=2 * len(omitted),
            ),
        )

    def recall_cards(self, result):
        """Snapshot approved full text from this space, never trust a foreign ID."""
        final = result.get("final", [])[:5]
        identifiers = list(dict.fromkeys(r["memory_id"] for r in final))
        with connect(self.dsn) as c:
            rows = (
                c.execute(
                    """SELECT id AS memory_id,title,content,source_date,item_kind,experience_mode
                FROM memory_core.memory_items WHERE bot_id=%s AND id=ANY(%s) AND archive_reason IS NULL""",
                    (self.space_id, identifiers),
                ).fetchall()
                if identifiers
                else []
            )
        by_id = {r["memory_id"]: r for r in rows}
        return [
            dict(by_id[mid], number=i + 1)
            for i, mid in enumerate(identifiers)
            if mid in by_id
        ]

    def save_context(self, record, body, source, recall):
        original = body["messages"][-1]
        persona = source["policy"].get("persona", "")
        systems = (
            [{"role": "system", "content": persona}]
            if persona.strip()
            else [
                thaw_message(freeze_message(m))
                for m in body["messages"]
                if m["role"] in ("system", "developer")
            ]
        )
        prefix = systems[:]
        sections = [{"kind": "persona", "start": 0, "end": len(prefix)}]
        # Empty sections are metadata only; no placeholder goes to a model.
        sections_with_text = memory_context_sections(source, source["turn"]["business_date"], self.settings)
        for kind, content in sections_with_text.items():
            start = len(prefix)
            if content:
                prefix.append({"role": "system", "content": content})
            sections.append(dict(kind=kind, start=start, end=len(prefix)))
        history, current_history_prefix, has_tool_records = _chat_history(source["history_groups"])
        if has_tool_records:
            start = len(prefix)
            prefix.append({"role": "system", "content": text.HISTORY_TOOL_RULE})
            sections.append(dict(kind="history_tool_rule", start=start, end=len(prefix)))
        start = len(prefix)
        prefix += history
        sections.append(dict(kind="history", start=start, end=len(prefix)))
        moment = datetime.fromisoformat(source["turn"]["created_at"]).astimezone(
            ZoneInfo(self.settings.timezone)
        )
        # Keep dynamic context out of middle system messages: some compatible
        # providers merge those into the initial system prefix. The dialogue
        # pair stays immediately before the real user and is frozen as a unit.
        index = len(prefix)
        supplement = text.SUPPLEMENT
        for kind, content in (
            (
                "history_omitted",
                (
                    text.HISTORY_OMITTED.format(
                        turns=source["history_omitted"]["turns"],
                        user_name=source["policy"]["participants"]["user"]["name"],
                    )
                    if source.get("history_omitted", {}).get("turns")
                    else ""
                ),
            ),
            (
                "recall",
                text.render_memories(
                    recall["cards"], source["policy"]["participants"]["user"]["name"]
                ),
            ),
            ("current_time", text.render_current_time(moment, self.settings.timezone)),
        ):
            if content:
                supplement += "\n\n"
                start = len(supplement)
                supplement += content
                sections.append(dict(
                    kind=kind, start=index, end=index + 1,
                    content_start=start, content_end=len(supplement),
                ))
            else:
                sections.append(dict(kind=kind, start=index, end=index))
        prefix += [
            {"role": "user", "content": supplement},
            {"role": "assistant", "content": text.CONTEXT_ACK},
        ]
        sections.append(dict(kind="context_ack", start=index + 1, end=index + 2))
        sections.append(
            dict(kind="current_message", start=len(prefix), end=len(prefix) + 1)
        )
        current = {
            "role": "user",
            "content": timed_content(
                original.get("content"), source["turn"]["created_at"], self.settings
            ),
        }
        frozen = dict(
            schema_version=2,
            frozen_at=source["turn"]["created_at"],
            prefix=[freeze_message(m) for m in prefix],
            user=freeze_message(original),
            current=freeze_message(current),
            parameters=deepcopy(
                {
                    k: v
                    for k, v in body.items()
                    if k not in ("messages", "stream", "stream_options")
                }
            ),
        )
        current_message = thaw_message(frozen["current"], original)
        if current_history_prefix:
            frozen["current_user_history_prefix"] = current_history_prefix
            current_message["content"] = prepend_text(current_message["content"], current_history_prefix)
        upstream = deepcopy(body)
        upstream["messages"] = [thaw_message(m) for m in frozen["prefix"]] + [current_message]
        manifest = dict(
            mode="hosted",
            schema_version=1,
            frozen_at=frozen["frozen_at"],
            policy_revision=source["policy"]["revision"],
            sections=sections,
            history_turn_ids=source["history_turn_ids"],
            history_message_ids=source["history_message_ids"],
            carryover_turn_ids=source["carryover_turn_ids"],
            recall_ids=[c["memory_id"] for c in recall["cards"]],
            summary_ids=[s["id"] for s in source.get("summaries", [])],
            diary_versions=[
                dict(id=d["id"], revision=d["revision"])
                for d in source.get("diaries", [])
            ],
            history_omitted=source.get(
                "history_omitted", dict(turn_ids=[], turns=0, messages=0)
            ),
        )
        with connect(self.dsn) as c:
            row = self._lock(c)
            if not self._owns(row, record):
                raise ConversationConflict(
                    "本轮已超时或占用失效，请重新发送", "turn_timeout"
                )
            c.execute(
                """UPDATE memory_core.conversation_turns SET frozen_context=%s,context_manifest=%s,recall_snapshot=%s
                WHERE bot_id=%s AND id=%s AND content_deleted_at IS NULL""",
                (
                    Jsonb(frozen),
                    Jsonb(manifest),
                    Jsonb(recall),
                    self.space_id,
                    record["turn_id"],
                ),
            )
        return upstream

    def _save_receipt(self, c, turn, record, reported, reported_none, timestamp):
        cards = turn["recall_snapshot"].get("cards", [])
        accepted = {card["memory_id"] for card in cards if card["number"] in reported}
        previous = c.execute(
            """SELECT r.usage_receipt FROM memory_core.conversation_requests r
            JOIN memory_core.conversation_turns t ON (t.id,t.bot_id)=(r.turn_id,r.bot_id)
            WHERE t.bot_id=%s AND t.position_id=%s""",
            (self.space_id, turn["position_id"]),
        ).fetchall()
        counted = {
            mid for r in previous for mid in r["usage_receipt"].get("counted_ids", [])
        }
        fresh = sorted(accepted - counted)
        updated = []
        if fresh:
            # Same order as other card-mutating transactions. Count + receipt
            # commit with the reply under the existing space/turn lock.
            owned = c.execute(
                "SELECT id FROM memory_core.memory_items WHERE bot_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE",
                (self.space_id, fresh),
            ).fetchall()
            updated = [r["id"] for r in owned]
            c.execute(
                "UPDATE memory_core.memory_items SET used_count=used_count+1,last_used_at=%s WHERE bot_id=%s AND id=ANY(%s)",
                (timestamp, self.space_id, updated),
            )
        status = "reported" if accepted else "reported_unused" if reported_none else "unreported"
        receipt = dict(
            status=status,
            reported_ids=sorted(accepted),
            counted_ids=updated,
            counted=bool(updated),
        )
        c.execute(
            "UPDATE memory_core.conversation_requests SET usage_receipt=%s WHERE id=%s AND bot_id=%s",
            (Jsonb(receipt), record["request_id"], self.space_id),
        )
