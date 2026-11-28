"""Space-key HTTP hooks for external chat clients and Claude Code."""

import json
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from starlette.concurrency import run_in_threadpool

from memory_core.conversations.chat_text import render_memories
from memory_core.conversations.conversation_rounds import ConversationConflict, fingerprint
from memory_core.conversations.hook_turns import HookTurns
from memory_core.storage.schema import HOOK_VERSION

from .proxy.proxy import MAX_BODY, reject_json_constant
from .proxy.usage_receipt import UsageFilter


LOG = logging.getLogger("memory_web.hooks")
EVENTS = {
    "/hooks/session-start": "SessionStart",
    "/hooks/user-prompt-submit": "UserPromptSubmit",
    "/hooks/stop": "Stop",
}
FIELDS = {
    "SessionStart": {"conversation_id"},
    "UserPromptSubmit": {"conversation_id", "turn_id", "message", "images"},
    "Stop": {"conversation_id", "turn_id", "reply"},
}
CONTEXT_LIMIT = 10_000


def _text(value, field):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(f"{field} 必须是有效文字")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{field} 必须是有效文字") from None
    return value


def _identifier(value, field):
    value = _text(value, field)
    if not value.strip() or len(value) > 128:
        raise ValueError(f"{field} 须为 1–128 个字符")
    return value


def _request(body, event):
    claude = "hook_event_name" in body
    if claude:
        if body["hook_event_name"] != event:
            raise ValueError("Claude Code 事件与接口路径不符")
        conversation = _identifier(body.get("session_id"), "session_id")
        turn = body.get("prompt_id")
        if turn is not None and event != "SessionStart":
            turn = _identifier(turn, "prompt_id")
    else:
        if body.keys() - FIELDS[event]:
            raise ValueError("请求包含不支持的字段")
        conversation = body.get("conversation_id")
        if conversation is not None:
            conversation = _identifier(conversation, "conversation_id")
        turn = body.get("turn_id")
        if event != "SessionStart":
            if type(turn) is int and 0 <= turn <= 2**53 - 1:
                turn = str(turn)
            turn = _identifier(turn, "turn_id")
    session_key = fingerprint([claude, conversation])
    prompt_key = fingerprint(turn) if turn is not None and event != "SessionStart" else None
    if event == "SessionStart":
        return claude, session_key, prompt_key, None
    if event == "Stop":
        field = "last_assistant_message" if claude else "reply"
        return claude, session_key, prompt_key, _text(body.get(field), field)

    field = "prompt" if claude else "message"
    message = _text(body.get(field, "") if not claude else body.get(field), field)
    images = [] if claude else body.get("images", [])
    if not isinstance(images, list):
        raise ValueError("images 必须是数组")
    if not message.strip() and not images:
        raise ValueError(f"{field} 和 images 至少提供一项")
    if not images:
        return claude, session_key, prompt_key, message

    parts = [{"type": "text", "text": message}] if message else []
    for image in images:
        if not isinstance(image, dict) or image.keys() - {"url", "detail"}:
            raise ValueError("图片格式无效")
        url = _text(image.get("url"), "url")
        if not url.startswith(("https://", "http://", "data:image/")):
            raise ValueError("图片须为 HTTP(S) URL 或 data:image/ URL")
        if "detail" in image and image["detail"] not in ("auto", "low", "high"):
            raise ValueError("图片 detail 格式无效")
        parts.append({"type": "image_url", "image_url": image})
    return claude, session_key, prompt_key, parts


def _clip(text, limit=CONTEXT_LIMIT):
    # JavaScript string length counts supplementary characters as two units.
    return text.encode("utf-16-le")[:limit * 2].decode("utf-16-le", errors="ignore")


def _claude_cards(cards, user_name):
    # Keep the wrapper and only record cards whose content reached Claude.
    visible = []
    for card in cards:
        rendered = render_memories([*visible, card], user_name, receipts=False)
        if _clip(rendered) == rendered:
            visible.append(card)
            continue
        framing = render_memories([*visible, dict(card, content="")], user_name, receipts=False)
        remaining = CONTEXT_LIMIT - len(framing.encode("utf-16-le")) // 2
        if remaining > 0 and (excerpt := _clip(card["content"], remaining)):
            visible.append(dict(card, content=excerpt))
        break
    return visible


def _claude(event, context=""):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": _clip(context)}}


def _error(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status)


class Hooks:
    def __init__(self, proxy, version):
        self.proxy = proxy
        self.version = version

    async def handle(self, request: Request):
        space, error = await self.proxy.authenticate(request)
        if error:
            return error
        if self.version < HOOK_VERSION:
            return _error(503, "hook_upgrade_required", "请先升级 Amber Heart 数据库")
        event = EVENTS[request.url.path]
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                return _error(413, "hook_body_too_large", "请求超过 16 MiB")
        try:
            body = json.loads(raw, parse_constant=reject_json_constant)
        except (ValueError, RecursionError):
            return _error(400, "invalid_hook_request", "请求必须是有效 JSON")
        try:
            if not isinstance(body, dict):
                raise ValueError("请求必须是 JSON 对象")
            claude, session, prompt, content = _request(body, event)
        except ValueError as exc:
            return _error(400, "invalid_hook_request", str(exc))

        try:
            store = await run_in_threadpool(HookTurns, self.proxy.settings.dsn, space)
            if event == "SessionStart":
                sections = await run_in_threadpool(
                    store.session_context, self.proxy.context.daily_settings.recent_diaries
                )
                text = "\n\n".join(filter(None, sections.values()))
                if claude:
                    return _claude(event, text)
                return dict(sections, context=text)
            if event == "UserPromptSubmit":
                text, cards = await self.recall(space, store, session, prompt, content, claude)
                if claude:
                    return _claude(event, text)
                return {"context": text, "cards": cards}
            receipt = UsageFilter()
            reply = receipt.feed(content) + receipt.finish()
            if not reply.strip():
                if claude:
                    return _claude(event)
                return _error(400, "invalid_hook_request", "reply 须包含正文")
            result = await run_in_threadpool(
                store.complete, session, prompt, reply,
                summary_trigger_count=self.proxy.summaries.policy.trigger_messages,
            )
            self.proxy.summaries.schedule(space, result["turn_id"])
            return _claude(event) if claude else result
        except ConversationConflict as exc:
            missing = exc.code == "hook_turn_missing"
            if missing and claude and event == "Stop":
                return _claude(event)
            return _error(404 if missing else 409, exc.code, str(exc))
        except (DatabaseError, OSError, ValueError) as exc:
            LOG.warning("hook_failed space=%s event=%s type=%s code=%s",
                        space, event, type(exc).__name__, getattr(exc, "sqlstate", None))
            return _error(503, "hook_storage_unavailable", "记忆暂不可用，请稍后重试")

    async def recall(self, space, store, session, prompt, content, claude):
        context = self.proxy.context
        record = await run_in_threadpool(store.reserve, session, prompt, content)
        snapshot = record["recall"]
        if record["created"] or snapshot["status"] == "not_run":
            self.proxy.images.schedule(space, record, {"role": "user", "content": content})
        if snapshot["status"] == "not_run":
            source = await run_in_threadpool(
                store.history_input,
                record,
                previous_messages=context.settings.previous_day_messages,
                max_history_chars=context.summary_settings.max_history_chars,
                recent_diary_count=0,
                hook_session=session,
            )
            name = source["policy"]["participants"]["user"]["name"]
            query = content if isinstance(content, str) else "".join(
                part["text"] for part in content if part["type"] == "text"
            )
            snapshot = await context.recall(store, query.strip(), source["history"])
            snapshot["hook_user_name"] = name
            if claude:
                snapshot["cards"] = _claude_cards(snapshot["cards"], name)
            snapshot = await run_in_threadpool(store.save_recall, record["turn_id"], snapshot)
        cards = snapshot["cards"]
        return render_memories(cards, snapshot["hook_user_name"], receipts=False), cards


def attach_hooks(app, version):
    hooks = Hooks(app.state.proxy, version)
    for path in EVENTS:
        app.add_api_route(path, hooks.handle, methods=["POST"])
