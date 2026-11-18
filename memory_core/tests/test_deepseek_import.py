"""Synthetic DeepSeek v1 archives with selected branches and ignored fragments."""

from base64 import b64decode
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from memory_core.imports.import_formats import ImportFormatError, parse_upload
from memory_core.storage.config import Settings


FIRST_DAY = "2025-03-14T18:59:00.123Z"
SECOND_DAY = "2025-03-14T19:01:00Z"
IMAGE_NAME = "synthetic-image.png"
IMAGE_BYTES = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=")


def _message(identifier, parent, role, fragments, *, stamp=FIRST_DAY, status="FINISHED"):
    return dict(id=identifier, parent_id=parent, role=role, status=status,
                inserted_at=stamp, thinking_enabled=role == "ASSISTANT",
                search_enabled=role == "ASSISTANT", fragments=fragments)


def deepseek_session(key="synthetic-chat", title="合成植物对话"):
    image = dict(export_path=f"chat_sessions/{key}_files/{IMAGE_NAME}", file_name=IMAGE_NAME, is_image=True)
    messages = [
        _message(1, None, "USER", [
            dict(type="FILE", files=[image]),
            dict(type="REQUEST", content="合成提问：照片里的植物如何照料？"),
        ]),
        _message(2, 1, "ASSISTANT", [dict(type="RESPONSE", content="合成弃用的第一次回复。")]),
        _message(3, 1, "ASSISTANT", [
            dict(type="THINK", content="合成内部思考。"),
            dict(type="TOOL_SEARCH", queries=["合成搜索词"], results=[]),
            dict(type="TOOL_OPEN", status="FINISHED", title="合成联网标题"),
            dict(type="RESPONSE", content="合成回复：先观察日照。"),
            dict(type="RESPONSE", content="再安排浇水。"),
            dict(type="CONTENT_RISK", style="WARNING", content="This response is AI-generated, for reference only."),
        ]),
        _message(4, 3, "USER", [dict(type="REQUEST", content="合成已经编辑掉的提问。")], stamp=SECOND_DAY),
        _message(5, 3, "USER", [dict(type="REQUEST", content="合成编辑后的提问。")], stamp=SECOND_DAY),
        _message(6, 5, "ASSISTANT", [
            dict(type="RESPONSE", content="合成超时前未完成的回复。"),
            dict(type="TEMPLATE_RESPONSE", content="服务器繁忙，请稍后再试。"),
        ], stamp=SECOND_DAY, status="TIMEOUT"),
        _message(7, 6, "USER", [dict(type="REQUEST", content="合成超时后的追问。")], stamp=SECOND_DAY),
        _message(8, 7, "ASSISTANT", [dict(type="RESPONSE", content="合成最终采用的回复。")], stamp=SECOND_DAY),
        _message(9, 7, "ASSISTANT", [dict(type="RESPONSE", content="合成编号更大的弃用回复。")], stamp=SECOND_DAY),
    ]
    return dict(id=key, title=title, inserted_at=FIRST_DAY, updated_at=SECOND_DAY,
                pinned=False, current_message_id=8, messages=messages)


def deepseek_zip(*sessions, version=3, omit=()):
    if not sessions:
        sessions = (deepseek_session(),)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        def write(name, payload):
            if name not in omit:
                archive.writestr(name, payload)

        write("manifest.json", json.dumps(dict(export_format_version=version)))
        write("user.json", b'{"phone_number":"SYNTHETIC_NOT_FOR_IMPORT"}')
        index = []
        for session in sessions:
            index.append(dict({k: session[k] for k in ("id", "title", "inserted_at", "updated_at", "pinned")},
                              message_count=len(session.get("messages", []))))
            write(f'chat_sessions/{session["id"]}.json', json.dumps(session, ensure_ascii=False))
            write(f'chat_sessions/{session["id"]}_files/{IMAGE_NAME}', IMAGE_BYTES)
        write("chat_session_index.json", json.dumps(index, ensure_ascii=False))
    return buffer.getvalue()


class DeepSeekImportTests(unittest.TestCase):
    def parse(self, data, **limits):
        return parse_upload("deepseek", [("deepseek_data.zip", data)], Settings(3), **limits)

    def test_selected_branch_is_split_by_business_day_and_ignores_other_payloads(self):
        opened = []
        real_open = ZipFile.open

        def only_chat_json(archive, member, *args, **kwargs):
            self.assertIn(member.filename, {
                "manifest.json", "chat_session_index.json", "chat_sessions/synthetic-chat.json",
            })
            opened.append(member.filename)
            return real_open(archive, member, *args, **kwargs)

        data = deepseek_zip()
        with patch.object(ZipFile, "open", only_chat_json):
            days = self.parse(data)
        self.assertEqual(opened, ["manifest.json", "chat_session_index.json", "chat_sessions/synthetic-chat.json"])
        self.assertEqual([day.business_date for day in days], ["2025-03-14", "2025-03-15"])
        self.assertEqual(days[0].segments, [
            dict(segment_id="m-1", speaker="user", text="合成提问：照片里的植物如何照料？",
                 timestamp="2025-03-15T02:59:00+08:00"),
            dict(segment_id="m-3", speaker="assistant", text="合成回复：先观察日照。\n再安排浇水。",
                 timestamp="2025-03-15T02:59:00+08:00"),
        ])
        self.assertEqual([segment["text"] for segment in days[1].segments], [
            "合成编辑后的提问。", "合成超时后的追问。", "合成最终采用的回复。",
        ])
        self.assertEqual(days[0].conversation_key, days[1].conversation_key)
        self.assertNotEqual(days[0].source_key, days[1].source_key)

    def test_unknown_version_and_missing_files_are_friendly_errors(self):
        for version in (1, 2, True, "3", None):
            with self.subTest(version=version), self.assertRaisesRegex(ImportFormatError, "格式版本 3"):
                self.parse(deepseek_zip(version=version))
        for name in ("manifest.json", "chat_session_index.json", "chat_sessions/synthetic-chat.json"):
            with self.subTest(name=name), self.assertRaisesRegex(ImportFormatError, "DeepSeek 导出格式不完整"):
                self.parse(deepseek_zip(omit=(name,)))

    def test_missing_message_fields_and_broken_current_branch_are_rejected(self):
        for field in ("parent_id", "role", "status", "inserted_at", "fragments"):
            session = deepseek_session()
            del session["messages"][0][field]
            with self.subTest(field=field), self.assertRaisesRegex(ImportFormatError, "DeepSeek 导出格式不完整"):
                self.parse(deepseek_zip(session))
        for parent in (8, 42):
            session = deepseek_session()
            session["messages"][0]["parent_id"] = parent
            with self.subTest(parent=parent), self.assertRaisesRegex(ImportFormatError, "断链或循环"):
                self.parse(deepseek_zip(session))

    def test_upload_and_expanded_json_limits_apply(self):
        data = deepseek_zip()
        with self.assertRaisesRegex(ImportFormatError, "DeepSeek 导出 ZIP 超过"):
            self.parse(data, max_bytes=len(data) - 1)
        with self.assertRaisesRegex(ImportFormatError, "解压后的 JSON 总量超过"):
            self.parse(data, max_json_bytes=1024)
