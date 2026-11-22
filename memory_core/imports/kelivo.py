"""Read the chat tables from Kelivo's SQLite backup."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
import sqlite3
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo
from zlib import error as DeflateError

import ijson

from .import_formats import ImportDay, ImportFormatError, MAX_JSON_BYTES, _hash, _identifier, _segment, _split


DATABASE_ENTRY = "database/kelivo.db"
MAX_DATABASE_BYTES = 1024 * 1024 * 1024
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ENCRYPTED = "请关闭备份加密后重新备份。"
LEGACY = "这份 Kelivo 备份使用旧版 chats.json 格式，请升级 Kelivo 后重新备份。"
UNREADABLE = "无法读取 Kelivo 的聊天数据库，文件可能已损坏或表结构不兼容，请重新备份。"
INVALID_MEMORY = "无法读取 Kelivo 的记忆条目，备份可能已损坏或格式不兼容，请重新备份。"
LEGACY_MEMORY_KEY = "assistant_memories_v1"


def _conversation_segments(db, key, selections, settings):
    messages = db.execute("""
        SELECT id, role, timestamp, message_order, group_id, version
        FROM message_rows WHERE conversation_id = ? ORDER BY message_order
    """, (key,))
    # The first revision fixes the group's position. A later regeneration can
    # reuse the original message ID as its group ID, even if the original had none.
    active = {}
    for message in messages:
        group = message["group_id"]
        if group is None:
            group = message["id"]
        version = message["version"]
        preference = (version == selections.get(group), version,
                      message["message_order"], message["id"])
        if group not in active or preference > active[group][0]:
            active[group] = (preference, message)

    for index, (_, message) in enumerate(active.values()):
        role = message["role"]
        if role not in ("user", "assistant"):
            continue
        parts = db.execute("""
            SELECT payload FROM message_part_rows
            WHERE revision_id = ? AND kind = 'text' ORDER BY ordinal
        """, (message["id"],))
        # Kelivo stores text payloads as raw strings and joins them directly.
        body = "".join(payload for (payload,) in parts).strip()
        if body:
            stamp = message["timestamp"]
            if type(stamp) is not int:
                raise ImportFormatError(UNREADABLE)
            yield _segment(role, body, EPOCH + timedelta(microseconds=stamp), index, settings)


def _assistant_names(db):
    # Chats-only backups can omit these rows. Project only the optional name
    # field so the importer never receives the assistant's configuration.
    try:
        rows = db.execute("""
            SELECT id, CASE WHEN json_valid(payload) THEN
                CASE WHEN json_type(payload, '$.name') = 'text' THEN json_extract(payload, '$.name') END
            END
            FROM assistant_rows
        """)
        return {identifier: name.strip() for identifier, name in rows if name and name.strip()}
    except sqlite3.OperationalError:
        return {}


def _settings_list(archive, key):
    try:
        member = archive.getinfo("settings.json")
    except KeyError:
        return []
    if member.flag_bits & 1:
        raise ImportFormatError(ENCRYPTED)
    if member.file_size > MAX_JSON_BYTES:
        raise ImportFormatError("Kelivo 备份的 settings.json 解压后超过 1 GiB 上限。")
    try:
        # Read only the named list. Kelivo exports business rows as a
        # JSON-encoded string in the settings object.
        with archive.open(member) as source:
            value = next(ijson.items(source, key), None)
        if value is None:
            return []
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ValueError
        return value
    except (ijson.JSONError, ValueError, RecursionError):
        raise ImportFormatError(INVALID_MEMORY) from None


def _settings_assistant_names(archive):
    return {row["id"]: row["name"].strip() for row in _settings_list(archive, "assistants_v1")
            if isinstance(row.get("id"), str) and isinstance(row.get("name"), str) and row["name"].strip()}


def _memory_days(entries, names, settings, *, legacy=False):
    zone = ZoneInfo(settings.timezone)
    for identifier, scope, assistant_id, status, content, stamp, source in entries:
        if status == "archived":
            continue
        if status != "active" or scope not in ("global", "assistant") or not isinstance(content, str):
            raise ImportFormatError(INVALID_MEMORY)
        body = content.strip()
        if not body:
            continue
        if legacy:
            day = None
            key = _hash("legacy:" + body)
        else:
            day = None
            if source in ("tool", "extracted"):
                if type(stamp) is not int:
                    raise ImportFormatError(INVALID_MEMORY)
                day = (EPOCH + timedelta(microseconds=stamp)).astimezone(zone).date().isoformat()
            key = _hash("entry:" + _identifier(identifier))
        if scope == "global":
            assistant_key, assistant_name = "global", "全局记忆"
        else:
            assistant_key = _hash(_identifier(assistant_id)) if assistant_id else None
            assistant_name = names.get(assistant_id)
        title = body if len(body) <= 120 else body[:120] + "…"
        yield ImportDay("kelivo", key, title, day, f"import:kelivo-memory:{key}",
                        [dict(segment_id="m-0", speaker="user", text=body, timestamp=None)],
                        assistant_key=assistant_key, assistant_name=assistant_name, is_memory_entry=True)


def _settings_memory_rows(archive):
    return ((row.get("id"), row.get("scope"), row.get("assistantId"), row.get("status", "active"),
             row.get("content"), row.get("createdAt"), row.get("source"))
            for row in _settings_list(archive, "memory_entries_v1"))


def _settings_legacy_rows(archive):
    return ((None, "assistant", row.get("assistantId"), "active", row.get("content"), None, None)
            for row in _settings_list(archive, LEGACY_MEMORY_KEY))


def _database_days(path, settings, archive):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        tables = {name for _, name, kind, *_ in db.execute("PRAGMA main.table_list") if kind == "table"}
        if not {"conversation_rows", "message_rows", "message_part_rows"} <= tables:
            raise ImportFormatError(UNREADABLE)
        db.row_factory = sqlite3.Row
        names = _assistant_names(db) if "assistant_rows" in tables else _settings_assistant_names(archive)
        conversations = db.execute("SELECT id, title, version_selections_json, assistant_id FROM conversation_rows")
        for identifier, title, selection_json, assistant_id in conversations:
            key = _identifier(identifier)
            selections = json.loads(selection_json)
            if not isinstance(selections, dict) or any(type(version) is not int for version in selections.values()):
                raise ImportFormatError(UNREADABLE)
            if not isinstance(title, str) or not title.strip():
                title = "未命名对话"
            segments = _conversation_segments(db, key, selections, settings)
            yield from _split("kelivo", key, title, segments, settings,
                              assistant_key=_hash(_identifier(assistant_id)) if assistant_id else None,
                              assistant_name=names.get(assistant_id))
        if "memory_entry_rows" in tables:
            entries = db.execute("""
                SELECT id, scope, assistant_id, status, content, entry_created_at,
                    CASE WHEN json_valid(payload) THEN json_extract(payload, '$.source') END
                FROM memory_entry_rows WHERE status = 'active'
            """)
        else:
            entries = _settings_memory_rows(archive)
        yield from _memory_days(entries, names, settings)
        if "assistant_memory_rows" in tables:
            entries = db.execute("""
                SELECT NULL, 'assistant', assistant_id, 'active', json_extract(payload, '$.content'), NULL, NULL
                FROM assistant_memory_rows ORDER BY sort_order, id
            """)
        else:
            entries = _settings_legacy_rows(archive)
        yield from _memory_days(entries, names, settings, legacy=True)


def kelivo_days(uploads, settings, temp_dir):
    for filename, stream in uploads:
        name = PurePosixPath(filename.lower())
        if stream.read(8) == b"KELIVENC" or name.suffix == ".kbackup":
            raise ImportFormatError(ENCRYPTED)
        stream.seek(0)
        if name.name == "chats.json":
            raise ImportFormatError(LEGACY)
        if name.suffix != ".zip":
            raise ImportFormatError("请选择 Kelivo 导出的 ZIP 备份文件。")

        try:
            with TemporaryDirectory(prefix="kelivo-", dir=temp_dir) as directory:
                database = Path(directory).absolute() / "kelivo.db"
                with ZipFile(stream) as archive:
                    members = [entry for entry in archive.infolist() if entry.filename == DATABASE_ENTRY]
                    if not members:
                        names = _settings_assistant_names(archive)
                        found = False
                        for legacy, entries in ((False, _settings_memory_rows(archive)),
                                                (True, _settings_legacy_rows(archive))):
                            for item in _memory_days(entries, names, settings, legacy=legacy):
                                found = True
                                yield item
                        if found:
                            continue
                        if "chats.json" in archive.namelist():
                            raise ImportFormatError(LEGACY)
                        raise ImportFormatError("备份里没有可导入的 Kelivo 聊天记录或记忆条目。")
                    if len(members) != 1:
                        raise ImportFormatError(UNREADABLE)
                    member = members[0]
                    if member.flag_bits & 1:
                        raise ImportFormatError(ENCRYPTED)
                    if member.file_size > MAX_DATABASE_BYTES:
                        raise ImportFormatError("Kelivo 聊天数据库解压后超过 1 GiB 上限。")
                    with archive.open(member) as source, database.open("wb") as output:
                        copyfileobj(source, output, length=65536)
                    yield from _database_days(database, settings, archive)
        except (BadZipFile, DeflateError, RuntimeError, OSError):
            raise ImportFormatError("无法读取 Kelivo 的 ZIP 文件，请重新备份。") from None
        except (sqlite3.Error, TypeError, UnicodeError, json.JSONDecodeError, OverflowError, RecursionError):
            raise ImportFormatError(UNREADABLE) from None
