"""Text from the selected branch of a DeepSeek v3 conversation."""

from datetime import datetime

from .import_formats import ImportFormatError, _segment, _split


FORMAT_ERROR = "DeepSeek 导出格式不完整或无法识别，请上传完整的官方 ZIP（格式版本 3）"


def _utc_time(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ImportFormatError(FORMAT_ERROR)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ImportFormatError(FORMAT_ERROR) from None


def _session_id(record):
    if not isinstance(record, dict):
        raise ImportFormatError(FORMAT_ERROR)
    key = record.get("id")
    if (not isinstance(key, str) or not key.strip() or key in (".", "..")
            or any(char in key for char in ("/", "\\", "\0"))):
        raise ImportFormatError(FORMAT_ERROR)
    if ("title" not in record or (record["title"] is not None and not isinstance(record["title"], str))
            or type(record.get("pinned")) is not bool):
        raise ImportFormatError(FORMAT_ERROR)
    _utc_time(record.get("inserted_at"))
    _utc_time(record.get("updated_at"))
    return key


def indexed_session_id(record):
    key = _session_id(record)
    count = record.get("message_count")
    if type(count) is not int or count < 0:
        raise ImportFormatError(FORMAT_ERROR)
    return key


def _selected_branch(session):
    messages = session.get("messages")
    current = session.get("current_message_id")
    if not isinstance(messages, list) or type(current) is not int:
        raise ImportFormatError(FORMAT_ERROR)
    by_id = {}
    for message in messages:
        identifier = message.get("id") if isinstance(message, dict) else None
        if type(identifier) is not int or identifier in by_id:
            raise ImportFormatError(FORMAT_ERROR)
        by_id[identifier] = message
    branch, seen = [], set()
    while current is not None:
        if type(current) is not int or current in seen or current not in by_id:
            raise ImportFormatError("DeepSeek 对话的当前分支存在断链或循环，请核对导出文件")
        seen.add(current)
        message = by_id[current]
        if "parent_id" not in message:
            raise ImportFormatError(FORMAT_ERROR)
        branch.append(message)
        current = message["parent_id"]
    return reversed(branch)


def session_days(session, key, settings):
    if _session_id(session) != key:
        raise ImportFormatError("DeepSeek 对话编号与目录不匹配，请上传完整的官方 ZIP")
    segments = []
    for message in _selected_branch(session):
        role, status, fragments = message.get("role"), message.get("status"), message.get("fragments")
        stamp = _utc_time(message.get("inserted_at"))
        if (role not in ("USER", "ASSISTANT") or status not in ("FINISHED", "TIMEOUT")
                or not isinstance(fragments, list)
                or type(message.get("thinking_enabled")) is not bool
                or type(message.get("search_enabled")) is not bool):
            raise ImportFormatError(FORMAT_ERROR)
        if status == "TIMEOUT":
            continue
        text_type = "REQUEST" if role == "USER" else "RESPONSE"
        parts = []
        for fragment in fragments:
            if not isinstance(fragment, dict) or not isinstance(fragment.get("type"), str):
                raise ImportFormatError(FORMAT_ERROR)
            if fragment["type"] == text_type:
                content = fragment.get("content")
                if not isinstance(content, str):
                    raise ImportFormatError(FORMAT_ERROR)
                parts.append(content)
        body = "\n".join(parts).strip()
        if body:
            segments.append(_segment(role.lower(), body, stamp, message["id"], settings))
    title = (session["title"] or "").strip() or "未命名对话"
    yield from _split("deepseek", key, title, segments, settings)
