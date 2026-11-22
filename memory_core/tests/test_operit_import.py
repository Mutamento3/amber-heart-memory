"""Synthetic projections of the Operit chat and memory exports."""

from datetime import datetime
import json
import unittest

from memory_core.conversations.chat_text import IMAGE_PLACEHOLDER
from memory_core.imports.import_formats import ImportFormatError, parse_upload
from memory_core.imports.import_preview import preview
from memory_core.tests.test_import_formats import SETTINGS, packed, uploaded


def epoch_ms(stamp):
    return int(datetime.fromisoformat(stamp).timestamp() * 1000)


def operit_message(sender, content, *, stamp="2025-03-14T02:59:59.500+08:00", selected=0, variants=()):
    return dict(baseMessage=dict(sender=sender, content=content, timestamp=epoch_ms(stamp),
                                 selectedVariantIndex=selected), variants=list(variants))


def operit_chat_export():
    first = dict(id="synthetic-chat-a", title="合成陶艺", createdAt="2025-03-14T02:34:56.789",
                 characterCardName="合成角色卡甲", parentChatId=None, characterGroupId=None,
                 messages=[
                     operit_message("user", "合成原话：今天开始学陶艺。"),
                     operit_message("ai", "合成未选回复", selected=2, variants=[
                         dict(variantIndex=2, content="合成选中回复。"),
                         dict(variantIndex=1, content="合成另一版。"),
                     ]),
                 ])
    second = dict(id="synthetic-chat-b", title="合成散步", createdAt="2025-03-14T08:20:02.123",
                  characterCardName="合成角色卡乙", parentChatId="synthetic-chat-a",
                  characterGroupId="synthetic-group", messages=[
                      operit_message("user", "合成原话：一起散步。", stamp="2025-03-14T08:21:00+08:00"),
                  ])
    return dict(archiveType="operit_chat_archive", formatVersion=2, chats=[first, second])


def operit_memory_export():
    def entry(key, title, source, content_type="text/plain"):
        return dict(uuid=key, title=title, content="合成记忆正文。", contentType=content_type,
                    source=source, createdAt=epoch_ms("2025-03-14T00:10:00+08:00"),
                    tagNames=["合成标签"], folderPath="合成目录")
    return dict(memories=[
        entry("synthetic-analysis", "合成自动抽取", "memory_analysis"),
        entry("synthetic-manual", "合成人工条目", "user_input"),
        entry("synthetic-merged", "合成合并条目", "merged_from_memory"),
        entry("synthetic-other", "合成其他条目", "other"),
        entry("synthetic-document", "合成文档片段", "memory_analysis", "application/pdf"),
    ], links=[dict(source="synthetic-analysis", target="synthetic-manual")])


def operit_zip():
    return packed([
        ("备份/聊天记录.JSON", json.dumps(operit_chat_export())),
        ("记忆库.json", json.dumps(operit_memory_export())),
        ("photos/ignored.png", b"synthetic attachment"),
    ])[0][1]


class OperitImportTests(unittest.TestCase):
    def test_message_variants_tools_attachments_and_exact_business_day(self):
        export = operit_chat_export()
        chat = export["chats"][0]
        chat["messages"] = [
            operit_message("user",
                '合成图片<link type=image id="synthetic-image">忽略图片引用</link>'
                '<attachment filename="test.jpg" type="image/jpeg">忽略附件引用</attachment>'
                '<attachment filename="A &amp; B.txt" type="text/plain">忽略文件路径</attachment>'
                '<link type="url">保留普通链接</link>'),
            operit_message("ai", "忽略未选版本", selected=2, variants=[
                dict(variantIndex=2, content=(
                    '<think>忽略思考\n内容</think><tool_A4-q name="calc">忽略工具</tool_A4-q>'
                    '<tool_result_qr8p status="ok">忽略结果</tool_result_qr8p>保留选中回复。')),
                dict(variantIndex=1, content="忽略另一版"),
            ]),
            operit_message("summary", "忽略摘要"),
            operit_message("ai", "<think>忽略仅有的思考</think>"),
            operit_message("ai", "保留基础版本。<think>忽略未闭合的思考",
                           stamp="2025-03-14T03:00:00+08:00",
                           variants=[dict(variantIndex=1, content="忽略未采用变体")]),
        ]
        export["chats"] = [chat]
        # Official top-level fields can come after the chats array.
        data = {key: export[key] for key in ("chats", "archiveType", "formatVersion")}
        previous, current = parse_upload("operit", uploaded(data, "合成聊天.json"), SETTINGS)
        self.assertEqual((previous.business_date, current.business_date), ("2025-03-13", "2025-03-14"))
        self.assertEqual([segment["speaker"] for segment in previous.segments], ["user", "assistant"])
        self.assertEqual([segment["text"] for segment in previous.segments], [
            "合成图片" + IMAGE_PLACEHOLDER * 2 + '[文件：A & B.txt]<link type="url">保留普通链接</link>',
            "保留选中回复。",
        ])
        self.assertEqual([segment["text"] for segment in current.segments], ["保留基础版本。"])
        self.assertEqual(previous.segments[0]["timestamp"], "2025-03-14T02:59:59+08:00")

    def test_memory_dates_follow_source_and_use_calendar_date_in_install_timezone(self):
        data = operit_memory_export()
        items = parse_upload("operit", uploaded(data, "library.json"), SETTINGS)
        self.assertEqual({item.title: item.business_date for item in items}, {
            "合成自动抽取": "2025-03-14", "合成人工条目": None, "合成合并条目": None, "合成其他条目": None,
        })
        self.assertTrue(all(item.is_memory_entry for item in items))
        self.assertTrue(all(item.segments[0]["text"] == item.title + "\n\n合成记忆正文。" for item in items))
        before = {item.title: item.source_key for item in items}
        data["memories"][0]["content"] = "合成改写后的正文。"
        updated = parse_upload("operit", uploaded(data, "renamed.json"), SETTINGS)
        self.assertEqual(before, {item.title: item.source_key for item in updated})

    def test_combined_zip_groups_chats_and_leaves_memories_unselected(self):
        items = parse_upload("operit", [("combined.zip", operit_zip())], SETTINGS)
        plan = preview(items, daily_limit=5)
        groups = {group["name"]: group["conversations"] for group in plan["assistant_groups"]}
        self.assertEqual(set(groups), {"合成角色卡甲", "合成角色卡乙"})
        self.assertEqual(plan["material_count"], 0)
        memories = plan["memory_groups"][0]["memories"]
        self.assertEqual(len(memories), 4)
        self.assertTrue(all(not memory["selected"] for memory in memories))
        key = groups["合成角色卡甲"][0]["key"]
        memory_key = next(entry["key"] for entry in memories if entry["source_date"] is None)
        chosen = preview(items, daily_limit=5, date_end="2025-03-13",
                         selected_conversations=[key], selected_memories=[memory_key])
        self.assertEqual((chosen["day_count"], chosen["message_count"], chosen["memory_count"]), (1, 2, 1))

    def test_invalid_version_and_missing_chosen_variant_are_rejected(self):
        data = operit_chat_export()
        data["formatVersion"] = 3
        with self.assertRaisesRegex(ImportFormatError, "版本 2"):
            parse_upload("operit", uploaded(data), SETTINGS)
        data["formatVersion"] = 2
        data["chats"][0]["messages"][1]["variants"] = []
        with self.assertRaisesRegex(ImportFormatError, "选用的回复版本"):
            parse_upload("operit", uploaded(data), SETTINGS)
