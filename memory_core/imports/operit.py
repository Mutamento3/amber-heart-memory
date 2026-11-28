"""Read Operit v2 chat exports and plain-text memory library entries."""

from datetime import datetime, timedelta, timezone
from html import unescape
import re
from zoneinfo import ZoneInfo

import ijson
from ijson.common import ObjectBuilder

from ..conversations.chat_text import IMAGE_PLACEHOLDER
from .import_formats import ImportDay, ImportFormatError, _array, _hash, _identifier, _object, _segment, _split


CHAT_ARCHIVE = "operit_chat_archive"
INVALID_EXPORT = "Operit 导出格式不完整，请重新导出聊天记录 JSON 或记忆库"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TAG_ATTRIBUTES = r"""(?:[^<>"']|"[^"]*"|'[^']*')*?"""
INTERNAL_BLOCK = re.compile(
    rf"<(?P<tag>think|tool_[^\s/<>]+)(?=[\s/>]){TAG_ATTRIBUTES}"
    r"(?:/>|>.*?(?:</(?P=tag)\s*>|\Z))",
    re.DOTALL,
)
ATTACHMENT = re.compile(
    rf"<(?P<tag>link|attachment)(?=[\s/>])(?P<attrs>{TAG_ATTRIBUTES})"
    r"(?:/>|>.*?</(?P=tag)\s*>)",
    re.DOTALL,
)
ATTRIBUTE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>]+))""")


def _instant(value):
    try:
        if type(value) is int:
            return EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        pass
    raise ImportFormatError("Operit 导出缺少有效的毫秒时间戳")


def _attachment_text(match):
    attributes = {
        key: unescape(double or single or bare or "")
        for key, double, single, bare in ATTRIBUTE.findall(match["attrs"])
    }
    kind = attributes.get("type", "").lower()
    if match["tag"] == "link":
        return IMAGE_PLACEHOLDER if kind == "image" else match[0]
    if kind.startswith("image/"):
        return IMAGE_PLACEHOLDER
    filename = attributes.get("filename", "").strip() or "未命名"
    return f"[文件：{filename}]"


def _selected_content(message, base):
    selected = base.get("selectedVariantIndex", 0)
    if type(selected) is not int or selected < 0:
        raise ImportFormatError(INVALID_EXPORT)
    if selected == 0:
        return base.get("content")
    for variant in _array(message.get("variants")):
        variant = _object(variant)
        if type(variant.get("variantIndex")) is int and variant["variantIndex"] == selected:
            return variant.get("content")
    raise ImportFormatError("Operit 导出缺少聊天中选用的回复版本，请重新导出")


def _segments(messages, settings):
    for index, message in enumerate(_array(messages)):
        message = _object(message)
        base = _object(message.get("baseMessage"))
        sender = base.get("sender")
        if sender not in ("user", "ai"):
            continue
        content = _selected_content(message, base)
        if not isinstance(content, str):
            raise ImportFormatError(INVALID_EXPORT)
        if sender == "ai":
            body = INTERNAL_BLOCK.sub("", content).strip()
            role = "assistant"
        else:
            body = ATTACHMENT.sub(_attachment_text, content).strip()
            role = "user"
        if body:
            yield _segment(role, body, _instant(base.get("timestamp")), index, settings)


def _chat_days(chat, settings):
    key = _identifier(chat.get("id"))
    title = chat.get("title")
    title = title.strip() if isinstance(title, str) else ""
    card = chat.get("characterCardName")
    card = card.strip() if isinstance(card, str) else ""
    # Message epochs are absolute; the chat's local createdAt is only metadata.
    return _split("operit", key, title or "未命名对话", _segments(chat.get("messages"), settings),
                  settings, assistant_key=_hash(card) if card else None, assistant_name=card or None)


def _memory_entries(entry, settings):
    if entry.get("contentType") != "text/plain":
        return
    content = entry.get("content")
    if not isinstance(content, str):
        raise ImportFormatError(INVALID_EXPORT)
    body = content.strip()
    if not body:
        return
    key = _hash(_identifier(entry.get("uuid")))
    title = entry.get("title")
    title = title.strip() if isinstance(title, str) else ""
    day = None
    if entry.get("source") == "memory_analysis":
        day = _instant(entry.get("createdAt")).astimezone(ZoneInfo(settings.timezone)).date().isoformat()
    text = f"{title}\n\n{body}" if title else body
    yield ImportDay("operit", key, title or body[:120], day, f"import:operit-memory:{key}",
                    [dict(segment_id="m-0", speaker="user", text=text, timestamp=None)],
                    assistant_key="library", assistant_name="记忆库", is_memory_entry=True)


def _records(events, field):
    """Buffer one record at a time, including when the header follows the array."""
    item = field + ".item"
    for prefix, event, _ in events:
        if (prefix, event) == (field, "end_array"):
            return
        if (prefix, event) != (item, "start_map"):
            raise ImportFormatError(INVALID_EXPORT)
        builder = ObjectBuilder()
        builder.event("start_map", None)
        for prefix, event, value in events:
            builder.event(event, value)
            if (prefix, event) == (item, "end_map"):
                yield builder.value
                break
    raise ImportFormatError(INVALID_EXPORT)


def operit_days(documents, settings):
    for document in documents:
        if document.peek(3).startswith(b"\xef\xbb\xbf"):
            document.read(3)
        try:
            events = ijson.parse(document, use_float=True)
            header, fields = {}, set()
            for prefix, event, field in events:
                if prefix or event != "map_key":
                    continue
                if field == "archiveType":
                    _, _, header[field] = next(events)
                if field not in ("chats", "memories"):
                    continue
                if fields or next(events)[:2] != (field, "start_array"):
                    raise ImportFormatError(INVALID_EXPORT)
                fields.add(field)
                parse = _chat_days if field == "chats" else _memory_entries
                for record in _records(events, field):
                    yield from parse(record, settings)
            if "chats" in fields or header.get("archiveType") == CHAT_ARCHIVE:
                # The export format version is not checked; the record
                # structure decides what can be read.
                if fields != {"chats"} or header.get("archiveType") != CHAT_ARCHIVE:
                    raise ImportFormatError("Operit 聊天记录导出格式无法识别，请上传官方导出的聊天记录 JSON")
        except (ijson.JSONError, UnicodeError, OverflowError, RecursionError, StopIteration):
            raise ImportFormatError(INVALID_EXPORT) from None
