"""Official exports → daily extraction material, without storage or model calls."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import re
from typing import Literal, get_args

from ..storage.clock import business_day_label, format_timestamp


MAX_UPLOAD_BYTES = 90 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024 * 1024
ImportSource = Literal["chatgpt", "claude", "gemini", "deepseek", "kelivo", "memory"]
SOURCES = frozenset(get_args(ImportSource))


class ImportFormatError(ValueError):
    """Safe to display: messages never include rejected personal content."""


def _hash(value):
    digest = sha256(value.encode("utf-8")).hexdigest()
    # Generated IDs also pass the ordinary draft credential screen.
    return ":".join(digest[i:i + 8] for i in range(0, len(digest), 8))


@dataclass
class ImportDay:
    source: str
    conversation_key: str
    title: str
    business_date: str | None
    source_key: str
    segments: list[dict] | None
    message_count: int | None = None
    assistant_key: str | None = None
    assistant_name: str | None = None
    is_kelivo_memory: bool = False
    memory_text: str | None = None

    def __post_init__(self):
        if self.message_count is None:
            self.message_count = len(self.segments)


def _object(value):
    if not isinstance(value, dict):
        raise ImportFormatError("导出文件中有格式不正确的记录")
    return value


def _array(value):
    if not isinstance(value, list):
        raise ImportFormatError("需要官方导出的 JSON 记录数组")
    return value


def _identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ImportFormatError("导出对话缺少来源编号，无法可靠防重")
    return value


def _timestamp(value, settings):
    try:
        if isinstance(value, datetime):
            instant = value
        elif type(value) in (int, float):
            instant = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError
        return format_timestamp(instant, settings)
    except (ValueError, OverflowError, OSError):
        raise ImportFormatError("文字消息缺少有效的带时区时间，无法按天切分") from None


def _segment(role, content, stamp, index, settings):
    return dict(segment_id=f"m-{index}", speaker=role, text=content,
                timestamp=_timestamp(stamp, settings))


def _chatgpt(conversation, settings):
    mapping = _object(conversation.get("mapping"))
    current = conversation.get("current_node")
    if not mapping:
        return []
    if not isinstance(current, str) or current not in mapping:
        raise ImportFormatError("ChatGPT 的 current_node 无效，无法确定采用分支")
    branch, seen = [], set()
    while current is not None:
        if not isinstance(current, str) or current in seen or current not in mapping:
            raise ImportFormatError("ChatGPT 采用分支存在断链或循环")
        seen.add(current)
        node = _object(mapping[current])
        branch.append(node.get("message"))
        current = node.get("parent")
    segments = []
    for index, message in enumerate(reversed(branch)):
        if message is None:
            continue
        message = _object(message)
        role = _object(message.get("author", {})).get("role")
        metadata = _object(message.get("metadata") or {})
        if (role not in ("user", "assistant")
                or metadata.get("is_visually_hidden_from_conversation")
                or message.get("channel") not in (None, "final", "commentary")
                or message.get("recipient") not in (None, "all")):
            continue
        content = _object(message.get("content") or {})
        if content.get("content_type") not in ("text", "multimodal_text"):
            continue
        parts = _array(content.get("parts", []))
        body = "\n".join(part for part in parts if isinstance(part, str)).strip()
        if body:
            segments.append(_segment(role, body, message.get("create_time"), index, settings))
    return segments


def _claude(conversation, settings):
    messages = conversation.get("chat_messages")
    if messages is None:
        messages = conversation.get("messages")
    segments = []
    for index, message in enumerate(_array(messages)):
        message = _object(message)
        role = message.get("sender") or message.get("role")
        role = "user" if role == "human" else role
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        body = ""
        if isinstance(content, list):
            body = "\n".join(block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str))
        if not body.strip():
            body = message.get("text")
            if body is None and isinstance(content, str):
                body = content
        if isinstance(body, str) and body.strip():
            segments.append(_segment(role, body.strip(), message.get("created_at"), index, settings))
    return segments


class _TakeoutText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden and tag in ("br", "p", "div", "li", "pre", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ("p", "div", "li", "pre", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _gemini(records, settings):
    for index, record in enumerate(records):
        record = _object(record)
        header = record.get("header", "")
        if "gemini" not in str(header).lower():
            continue
        subtitles = record.get("subtitles") or []
        prompt = next((part["value"] for part in _array(subtitles)
            if isinstance(part, dict) and part.get("name") in ("You", "你", "您")
            and isinstance(part.get("value"), str)), None)
        if prompt is None:
            title = record.get("title", "")
            if not isinstance(title, str):
                raise ImportFormatError("Gemini 提问格式不正确")
            match = re.match(r"^(?:Prompted(?: with)?:?\s+|提问了[：:]?\s*|提示了[：:]?\s*)(.+)$", title, re.S)
            if match:
                prompt = match[1]
            elif record.get("safeHtmlItem"):
                # Keep a localized title intact rather than discard its answer.
                prompt = title
        if not prompt or not prompt.strip():
            continue
        stamp = record.get("time")
        yield _segment("user", prompt.strip(), stamp, index * 2, settings)
        answers = record.get("safeHtmlItem") or []
        parser = _TakeoutText()
        for answer in _array(answers):
            html = _object(answer).get("html")
            if isinstance(html, str):
                parser.feed(html)
                parser.parts.append("\n")
        parser.close()
        body = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()
        if body:
            yield _segment("assistant", body, stamp, index * 2 + 1, settings)


def _split(source, key, title, segments, settings, *, assistant_key=None, assistant_name=None):
    days = {}
    for segment in segments:
        label = business_day_label(datetime.fromisoformat(segment["timestamp"]), settings).isoformat()
        days.setdefault(label, []).append(segment)
    conversation_key = _hash(key)
    return [ImportDay(source, conversation_key, title, day,
        f"import:{source}:{conversation_key}:{day}", messages,
        assistant_key=assistant_key, assistant_name=assistant_name)
        for day, messages in sorted(days.items())]


def _memory(source):
    day, lines, fence = None, [], None

    def finish():
        content = "\n".join(lines).strip()
        if day is not None and not content:
            raise ImportFormatError("记忆日期标题后必须有正文")
        if content:
            digest = _hash(content)
            return ImportDay("memory", digest, day, day,
                f"import:memory:{day}:{digest}",
                [dict(segment_id="m-0", speaker="user", text=content, timestamp=None)])

    for raw_line in source:
        line = raw_line.rstrip("\r\n")
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        heading = None if fence else re.fullmatch(r"##\s+(\d{4}-\d{2}-\d{2})\s*", line)
        if heading:
            item = finish()
            if item is not None:
                yield item
            try:
                day = date.fromisoformat(heading[1]).isoformat()
            except ValueError:
                raise ImportFormatError("记忆日期必须是有效的 YYYY-MM-DD") from None
            lines = []
        elif day is None and line.strip():
            raise ImportFormatError("记忆文件每条需以 ## YYYY-MM-DD 开头")
        else:
            lines.append(line)
    item = finish()
    if item is not None:
        yield item


def parse_upload(source, uploads, settings, *, max_bytes=MAX_UPLOAD_BYTES,
                 max_json_bytes=MAX_JSON_BYTES, temp_dir=None):
    """Explicit small-input convenience; production consumes iter_upload directly."""
    from .import_stream import iter_upload
    return sorted(iter_upload(source,uploads,settings,max_bytes=max_bytes,
        max_json_bytes=max_json_bytes,temp_dir=temp_dir),
        key=lambda item:(item.business_date is None,item.business_date or "",item.conversation_key))
