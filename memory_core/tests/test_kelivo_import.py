"""Kelivo v3 archives created from synthetic rows, with no app data."""

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from memory_core.imports.import_formats import ImportFormatError, parse_upload
from memory_core.imports.import_preview import preview
from memory_core.tests.test_import_formats import SETTINGS, packed


# Projection of Kelivo's drift_schema_v5.json at 83d336c, including the
# required timestamps and keys for the three tables the importer reads.
SCHEMA = """
CREATE TABLE conversation_rows (
    id TEXT NOT NULL PRIMARY KEY, title TEXT NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    assistant_id TEXT,
    version_selections_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE message_rows (
    id TEXT NOT NULL PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversation_rows(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IS NOT ''), timestamp INTEGER NOT NULL,
    group_id TEXT, version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    message_order INTEGER NOT NULL CHECK (message_order >= 0),
    UNIQUE (conversation_id, message_order), UNIQUE (conversation_id, group_id, version)
);
CREATE TABLE message_part_rows (
    part_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0), kind TEXT NOT NULL CHECK (kind IS NOT ''),
    payload TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE (revision_id, ordinal),
    FOREIGN KEY (revision_id) REFERENCES message_rows(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    CHECK (updated_at >= created_at)
);
"""

ASSISTANT_SCHEMA = """
CREATE TABLE assistant_rows (
    id TEXT NOT NULL PRIMARY KEY, sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
    payload TEXT NOT NULL, updated_at INTEGER NOT NULL
);
"""

MEMORY_SCHEMA = """
CREATE TABLE memory_entry_rows (
    id TEXT NOT NULL PRIMARY KEY, sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
    scope TEXT NOT NULL CHECK (scope IN ('global', 'assistant')), assistant_id TEXT,
    type TEXT NOT NULL CHECK (type IN ('identity', 'workflow', 'voice', 'instruction')),
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    content TEXT NOT NULL, content_normalized TEXT NOT NULL,
    entry_created_at INTEGER NOT NULL, entry_updated_at INTEGER NOT NULL,
    payload TEXT NOT NULL, updated_at INTEGER NOT NULL,
    CHECK ((scope = 'global' AND assistant_id IS NULL) OR (scope = 'assistant' AND assistant_id IS NOT NULL)),
    CHECK (entry_updated_at >= entry_created_at)
);
CREATE TABLE assistant_memory_rows (
    id TEXT NOT NULL PRIMARY KEY, sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
    assistant_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at INTEGER NOT NULL
);
"""


def _add_memories(db, stamp, *, revised):
    db.executescript(MEMORY_SCHEMA)
    entries = [
        ("mem_global", "global", None, "instruction", "active", "tool", "合成全局记忆。" + "合成续文。" * 36),
        ("mem_global_other", "global", None, "identity", "active", "manual", "合成另一位助手的全局记忆。"),
        ("mem_assistant", "assistant", "synthetic-a", "workflow", "active", "extracted",
         "合成陶艺记忆已编辑。" if revised else "合成陶艺记忆。"),
        ("mem_archived", "assistant", "synthetic-a", "voice", "archived", "distilled", "合成归档条目。"),
    ]
    db.executemany("INSERT INTO memory_entry_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        (key, order, scope, assistant, kind, status, content, content, stamp, stamp,
         json.dumps(dict(source=source)), stamp)
        for order, (key, scope, assistant, kind, status, source, content) in enumerate(entries)
    ))
    old_id = 27 if revised else 4
    db.execute("INSERT INTO assistant_memory_rows VALUES (?, 0, ?, ?, ?)", (
        str(old_id), "synthetic-b",
        json.dumps(dict(id=old_id, assistantId="synthetic-b", content="合成旧记忆。")), stamp,
    ))


def kelivo_backup(*, incompatible=False, multiple_assistants=False, assistant_table=True,
                  with_memories=False, revised=False):
    before = int(datetime.fromisoformat("2025-03-14T03:00:00+08:00").timestamp()) * 1_000_000 - 1
    after = before + 1
    key = "synthetic-kelivo"
    with TemporaryDirectory() as temp:
        path = Path(temp) / "kelivo.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            db.execute("INSERT INTO conversation_rows VALUES (?, ?, ?, ?, ?, ?)", (
                key, "合成对话", before, after, "synthetic-a" if multiple_assistants else None,
                json.dumps({"a": 0, "stale": 41}),
            ))
            messages = [
                ("u", "user", before, None, 0),
                ("a", "assistant", before, None, 0),
                ("default-old", "assistant", after, "default", 0),
                ("stale-old", "assistant", after, "stale", 0),
                ("a-new", "assistant", after, "a", 1),
                ("stale-new", "assistant", after, "stale", 1),
                ("default-new", "assistant", after, "default", 2),
                ("attachment", "user", after, None, 0),
                ("system", "system", after, None, 0),
            ]
            db.executemany("INSERT INTO message_rows VALUES (?, ?, ?, ?, ?, ?, ?)", (
                (identifier, key, role, stamp, group, version, order)
                for order, (identifier, role, stamp, group, version) in enumerate(messages)
            ))
            parts = [
                ("u", 2, "text", "一起学陶艺。"),
                ("u", 1, "image", '{"uri":"synthetic.png"}'),
                ("u", 0, "text", "合成原话："),
                ("a", 0, "reasoning", "合成思考"),
                ("a", 1, "tool_call", '{"synthetic":true}'),
                ("a", 2, "text", "合成选中回复。"),
                ("a", 3, "file", '{"uri":"synthetic.txt","name":"synthetic.txt"}'),
                ("a-new", 0, "text", "未选中的新版本。"),
                ("default-old", 0, "text", "没有选中的旧版本。"),
                ("default-new", 0, "text", "缺省取最新版本。"),
                ("stale-old", 0, "text", "选择失效的旧版本。"),
                ("stale-new", 0, "text", "选择失效时取最新版本。"),
                ("attachment", 0, "image", '{"uri":"image-only.png"}'),
                ("system", 0, "text", "合成系统提示词。"),
            ]
            db.executemany("""INSERT INTO message_part_rows
                (conversation_id, revision_id, ordinal, kind, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (
                (key, *part, before, after) for part in parts
            ))
            if multiple_assistants:
                if assistant_table:
                    db.executescript(ASSISTANT_SCHEMA)
                    db.executemany("INSERT INTO assistant_rows VALUES (?, ?, ?, ?)", (
                        ("synthetic-a", 0, json.dumps(dict(name="陶艺助手", systemPrompt="合成助手配置")), after),
                        ("synthetic-b", 1, json.dumps(dict(name="写作助手")), after),
                    ))
                for identifier, title, assistant_id, text in (
                    ("tea", "合成茶杯记录", "synthetic-a", "合成原话：我做了一只茶杯。"),
                    ("writing", "合成写作记录", "synthetic-b", "合成原话：这是另一位助手的聊天。"),
                    ("unassigned", "合成未归属记录", None, "合成原话：这段对话没有关联助手。"),
                ):
                    db.execute("INSERT INTO conversation_rows VALUES (?, ?, ?, ?, ?, ?)", (
                        identifier, title, before, before, assistant_id, "{}",
                    ))
                    db.execute("INSERT INTO message_rows VALUES (?, ?, ?, ?, ?, ?, ?)", (
                        identifier, identifier, "user", before, None, 0, 0,
                    ))
                    db.execute("""INSERT INTO message_part_rows
                        (conversation_id, revision_id, ordinal, kind, payload, created_at, updated_at)
                        VALUES (?, ?, 0, 'text', ?, ?, ?)""", (identifier, identifier, text, before, before))
            if incompatible:
                db.execute("ALTER TABLE message_part_rows RENAME COLUMN payload TO different_payload")
            if with_memories:
                _add_memories(db, before, revised=revised)
        return packed([
            ("manifest.json", '{"format":"kelivo-backup","formatVersion":3,"includeChats":true}'),
            ("settings.json", '{"systemPrompt":"合成提示词快照"}'),
            ("database/kelivo.db", path.read_bytes()),
            ("files/synthetic.txt", "合成附件内容"),
        ])[0][1]


class KelivoImportTests(unittest.TestCase):
    def test_reads_selected_revisions_and_only_text_in_timeline_order(self):
        backup = kelivo_backup()
        items = parse_upload("kelivo", [("sandbox.zip", backup), ("renamed.zip", backup)], SETTINGS)
        self.assertEqual([item.business_date for item in items], ["2025-03-13", "2025-03-14"])
        self.assertEqual(len({item.conversation_key for item in items}), 1)
        self.assertEqual([segment["text"] for item in items for segment in item.segments], [
            "合成原话：一起学陶艺。", "合成选中回复。", "缺省取最新版本。", "选择失效时取最新版本。",
        ])
        self.assertEqual([s["speaker"] for s in items[0].segments], ["user", "assistant"])
        self.assertEqual(items[0].segments[0]["timestamp"], "2025-03-14T02:59:59+08:00")
        self.assertEqual(items[1].segments[0]["timestamp"], "2025-03-14T03:00:00+08:00")
        plan = preview(items, daily_limit=12, date_end="2025-03-13")
        self.assertEqual(plan["source_keys"], [items[0].source_key])
        self.assertEqual(plan["conversations"], [])
        self.assertEqual(plan["message_count"], 2)
        duplicate = preview(items, daily_limit=12, date_end="2025-03-13", imported_keys=plan["source_keys"])
        self.assertEqual(duplicate["skipped_count"], 1)
        self.assertEqual(duplicate["source_keys"], [])

    def test_missing_assistant_table_keeps_distinct_groups_with_title_examples(self):
        backup = kelivo_backup(multiple_assistants=True, assistant_table=False)
        items = parse_upload("kelivo", [("chats-only.zip", backup)], SETTINGS)
        plan = preview(items, daily_limit=12)
        groups = {group["name"]: group for group in plan["assistant_groups"]}
        self.assertEqual(set(groups), {"助手 1", "助手 2", "未归属"})
        self.assertEqual(plan["material_count"], 0)
        self.assertEqual({frozenset(groups[name]["sample_titles"]) for name in ("助手 1", "助手 2")}, {
            frozenset(("合成对话", "合成茶杯记录")), frozenset(("合成写作记录",)),
        })
        self.assertEqual([item["title"] for item in groups["未归属"]["conversations"]], ["合成未归属记录"])

    def test_encrypted_backup_has_actionable_error_even_when_renamed(self):
        for filename in ("encrypted.kbackup", "renamed.zip"):
            with self.subTest(filename=filename), self.assertRaisesRegex(ImportFormatError, "请关闭备份加密后重新备份"):
                parse_upload("kelivo", [(filename, b"KELIVENC\x03" + bytes(64))], SETTINGS)

    def test_memory_entries_group_by_assistant_and_global_with_independent_selection(self):
        backup = kelivo_backup(multiple_assistants=True, with_memories=True)
        items = parse_upload("kelivo", [("mixed.zip", backup)], SETTINGS)
        plan = preview(items, daily_limit=12)
        groups = {group["name"]: group["memories"] for group in plan["memory_groups"]}
        self.assertEqual(set(groups), {"陶艺助手", "写作助手", "全局记忆"})
        self.assertEqual(plan["memory_count"], 0)
        self.assertEqual(sum(len(entries) for entries in groups.values()), 4)
        self.assertFalse(any(entry["selected"] for entries in groups.values() for entry in entries))
        self.assertEqual(groups["陶艺助手"][0]["source_date"], "2025-03-14")
        self.assertIsNone(groups["写作助手"][0]["source_date"])
        self.assertEqual([entry["source_date"] for entry in groups["全局记忆"]], ["2025-03-14", None])
        chosen = [groups[name][0]["key"] for name in ("陶艺助手", "全局记忆")]
        filtered = preview(items, daily_limit=12, selected_conversations=[], selected_memories=chosen,
                           date_end="2025-03-13")
        self.assertEqual(filtered["memory_count"], 2)
        self.assertEqual(filtered["message_count"], 0)
        self.assertEqual(filtered["model_calls_min"], filtered["model_calls_max"])
        self.assertEqual(filtered["model_calls_max"], 2)

    def test_legacy_settings_zip_can_restore_memory_after_chats_are_deleted(self):
        settings = dict(
            assistants_v1=json.dumps([dict(id="assistant-legacy", name="旧版助手")]),
            assistant_memories_v1=json.dumps([
                dict(id=10, assistantId="assistant-legacy", content="合成旧版记忆。"),
                dict(id=11, assistantId="assistant-legacy", content="合成旧版记忆。"),
            ]),
        )
        items = parse_upload("kelivo", packed([("chats.json", "{}"), ("settings.json", json.dumps(settings))]), SETTINGS)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].business_date)
        plan = preview(items, daily_limit=12)
        self.assertIsNone(plan["first_date"])
        self.assertEqual(plan["memory_groups"][0]["name"], "旧版助手")

    def test_settings_memory_entries_use_dates_only_for_tool_and_extracted_sources(self):
        stamp = int(datetime.fromisoformat("2025-03-14T00:01:00+08:00").timestamp()) * 1_000_000
        entries = [dict(id=source, scope="global", source=source, content=source, createdAt=stamp)
                   for source in ("tool", "extracted", "manual", "distilled")]
        backup = packed([("settings.json", json.dumps(dict(memory_entries_v1=json.dumps(entries))))])
        items = parse_upload("kelivo", backup, SETTINGS)
        self.assertEqual({entry.title: entry.business_date for entry in items}, {
            "tool": "2025-03-14", "extracted": "2025-03-14", "manual": None, "distilled": None,
        })

    def test_legacy_backup_requests_upgrade(self):
        uploads = packed([("chats.json", "{}"), ("settings.json", "{}")])
        with self.assertRaisesRegex(ImportFormatError, "请升级 Kelivo 后重新备份"):
            parse_upload("kelivo", uploads, SETTINGS)

    def test_incompatible_schema_reports_error_and_cleans_up(self):
        backup = kelivo_backup(incompatible=True)
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ImportFormatError, "表结构不兼容"):
                parse_upload("kelivo", [("incompatible.zip", backup)], SETTINGS, temp_dir=temp)
            self.assertEqual(list(Path(temp).iterdir()), [])
