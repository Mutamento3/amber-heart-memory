from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import psycopg
from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore
from memory_core.storage.db import connect
from memory_core.storage.install import install, MIGRATIONS, catalog_fingerprint
from memory_core.storage.space import SpaceConfiguration, Participant, RevisionConflict
from memory_core.tests.support import DatabaseTestCase


def body(value, systems=()):
    return dict(
        model="synthetic",
        messages=[*deepcopy(systems), {"role": "user", "content": value}],
    )


class HostedHistoryTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        self.settings = Settings(3, "America/New_York", 4)
        install(self.owner, self.settings)
        self.config = SpaceConfiguration(self.owner)
        self.config.configure("one", Participant("用户"), Participant("助手"))
        self.config.configure("two", Participant("用户"), Participant("另一助手"))
        self.store = ConversationStore(self.app, "one")
        self.now = datetime.fromisoformat("2026-11-02T05:00:00-05:00")

    def tearDown(self):
        pass

    def start(self, value, *, now=None, intent=None):
        request = body(value)
        record = self.store.begin(
            request, intent or {"operation": "new"}, now=now or self.now
        )
        return request, record

    def save(self, value, reply="合成回复", **kwargs):
        request, record = self.start(value, **kwargs)
        self.store.finish(
            record,
            content=reply,
            finish_reason="stop",
            now=kwargs.get("now") or self.now,
        )
        return record

    def snapshot(self, request, record, cards=()):
        source = self.store.history_input(record)
        return self.store.save_context(
            record, request, source, {"status": "ok", "cards": list(cards)}
        )

    def test_persona_revision_concurrent_and_application_read_only(self):
        policy = self.config.set_persona(
            "one", expected_revision=1, persona="中性人设\n第二行"
        )
        self.assertEqual(
            (policy["persona"], policy["revision"]), ("中性人设\n第二行", 2)
        )
        with self.assertRaises(RevisionConflict):
            self.config.set_persona("one", expected_revision=1, persona="旧页面")
        self.config.configure(
            "one", Participant("新用户"), Participant("助手"), expected_revision=2
        )
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT persona FROM memory_core.space_policies WHERE bot_id='one'"
                ).fetchone()["persona"],
                policy["persona"],
            )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege), connect(
            self.app
        ) as c:
            c.execute("UPDATE memory_core.space_policies SET persona='越权'")
        cleared = self.config.set_persona("one", expected_revision=3, persona="  \n")
        self.assertEqual(cleared["persona"], "")

    def test_upgrade_v10_data_repeat_and_transactional_failure(self):
        # A separate fixture starts from the actual immutable 3B migration.
        with connect(self.owner) as c:
            c.execute("DROP SCHEMA memory_core CASCADE")
        install(self.owner, self.settings, target_version=10)
        self.config.configure("old", Participant("用户"), Participant("助手"))
        with connect(self.owner) as c:
            before = catalog_fingerprint(c)
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(directory) / "migrations"
            shutil.copytree(MIGRATIONS, migrations)
            path = migrations / "011_hosted_context.sql"
            path.write_text(path.read_text() + "\nSELECT 1/0;")
            with patch("memory_core.storage.install.MIGRATIONS", migrations), self.assertRaises(
                psycopg.errors.DivisionByZero
            ):
                install(self.owner, self.settings)
        with connect(self.owner) as c:
            self.assertEqual(catalog_fingerprint(c), before)
        self.assertTrue(install(self.owner, self.settings))
        self.assertFalse(install(self.owner, self.settings))
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT persona,revision FROM memory_core.space_policies WHERE bot_id='old'"
                ).fetchone(),
                {"persona": "", "revision": 1},
            )

    def test_adopted_versions_exclude_replacement_old_and_other_space(self):
        old = self.save("旧版提问", reply="旧版回答")
        request, candidate = self.start(
            "改写提问", intent={"operation": "edit", "target_turn_id": old["turn_id"]}
        )
        upstream = self.snapshot(request, candidate)
        self.assertNotIn("旧版", str(upstream))
        self.store.finish(
            candidate, content="新版回答", finish_reason="stop", now=self.now
        )
        other = ConversationStore(self.app, "two")
        record = other.begin(body("另一空间私有正文"), {}, now=self.now)
        other.finish(record, content="空间二回答", finish_reason="stop", now=self.now)
        request, current = self.start("新话")
        upstream = self.snapshot(request, current)
        self.assertIn("改写提问", str(upstream))
        self.assertIn("新版回答", str(upstream))
        self.assertNotIn("旧版", str(upstream))
        self.assertNotIn("空间二", str(upstream))

    def test_errors_partials_all_tool_preambles_and_images(self):
        request, failed = self.start("未获回应")
        self.store.finish(failed, state="upstream_failed", now=self.now)
        request, partial = self.start(
            [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,NOT_SAVED"},
                }
            ]
        )
        self.store.finish(partial, content="半段", state="interrupted", now=self.now)
        request, tools = self.start("请查工具")
        call = {
            "id": "call-one",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        self.store.finish(
            tools,
            content="调用前正文",
            tools=[call],
            finish_reason="tool_calls",
            now=self.now,
        )
        continuation = deepcopy(request)
        continuation["messages"] += [
            {"role": "assistant", "content": "调用前正文", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call-one", "content": "不回放工具秘密"},
        ]
        step = self.store.begin(continuation, {}, now=self.now)
        self.store.finish(step, content="最终正文", finish_reason="stop", now=self.now)
        with connect(self.app) as c:
            c.execute(
                "UPDATE memory_core.conversation_tool_events SET reported_status='failure' WHERE kind='result'"
            )
        request, last = self.start("然后呢")
        history = self.snapshot(request, last)["messages"]
        saved = str(history)
        for expected in (
            "未获回应",
            "未完成",
            "图片内容未保留",
            "调用前正文",
            "最终正文",
            "lookup（已返回）",
        ):
            self.assertIn(expected, saved)
        for missing in ("不回放工具秘密", "NOT_SAVED", "tool_calls"):
            self.assertNotIn(missing, saved)

    def test_failure_after_tool_preamble_marks_the_whole_round_unfinished(self):
        request, first = self.start("调用后失败")
        call = {
            "id": "fail-tool",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        self.store.finish(
            first,
            content="先查一下",
            tools=[call],
            finish_reason="tool_calls",
            now=self.now,
        )
        request["messages"] += [
            {"role": "assistant", "content": "先查一下", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "fail-tool", "content": "结果"},
        ]
        step = self.store.begin(request, {}, now=self.now)
        self.store.finish(step, state="upstream_failed", now=self.now)
        request, next_round = self.start("接着聊")
        upstream = self.snapshot(request, next_round)
        self.assertIn("先查一下\\n[本轮调用了工具", str(upstream))
        self.assertIn("这轮回复未完成", str(upstream))

    def test_failed_empty_interruption_and_partial_have_exclusive_markers(self):
        from memory_core.conversations import chat_text
        for question, state, answer in (("失败前没收到正文", "upstream_failed", ""),
                ("中断前没收到正文", "interrupted", ""),
                ("中断前收到半段", "interrupted", "半段回答")):
            _, record = self.start(question)
            self.store.finish(record, content=answer, state=state, now=self.now)
        request, record = self.start("继续聊天")
        history = self.snapshot(request, record)["messages"]
        self.assertEqual(str(history).count(chat_text.UNANSWERED), 2)
        self.assertEqual(str(history).count(chat_text.INCOMPLETE), 1)
        partial_user = next(m for m in history if "中断前收到半段" in m["content"])
        self.assertNotIn(chat_text.UNANSWERED, partial_user["content"])

    def test_today_and_previous_day_carryover_and_cache_prefix_bytes(self):
        yesterday = self.now - timedelta(days=1)
        self.save("前日第一句", reply="前日回复一", now=yesterday)
        self.save(
            "前日第二句", reply="前日回复二", now=yesterday + timedelta(minutes=1)
        )
        request, first = self.start("当天一句")
        first_upstream = self.snapshot(request, first)
        stable = first_upstream["messages"][
            :-3
        ]  # fixed history before current time/user
        self.assertEqual(len(stable), 4)
        self.store.finish(
            first, content="当天回复一", finish_reason="stop", now=self.now
        )
        request, second = self.start("当天第二句", now=self.now + timedelta(minutes=2))
        second_upstream = self.snapshot(request, second)
        import json

        encode = lambda v: json.dumps(
            v, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertEqual(
            encode(stable), encode(second_upstream["messages"][: len(stable)])
        )
        self.assertIn("[11-02 05:00] 当天一句", str(second_upstream))
        self.assertIn("America/New_York", str(second_upstream))

    def test_supplementary_pair_contains_configured_name_time_and_frozen_card_text(self):
        from memory_core.conversations import chat_text
        self.config.configure("one", Participant("小岚"), Participant("助手"), expected_revision=1)
        request = body("真实用户消息", [{"role": "system", "content": "前端系统词"}])
        record = self.store.begin(request, {"operation": "new"}, now=self.now)
        cards = [dict(number=1, memory_id=1, content="合成卡片正文", experience_mode="shared")]
        upstream = self.snapshot(request, record, cards)
        self.assertEqual([m["role"] for m in upstream["messages"]], ["system", "user", "assistant", "user"])
        context, ack, current = upstream["messages"][-3:]
        self.assertIn("以下是和小岚当前话题", context["content"])
        self.assertIn("共同的赛博经历（真实的关系互动", context["content"])
        self.assertIn("当前时间：2026-11-02 周一 05:00（America/New_York）", context["content"])
        self.assertEqual(ack["content"], chat_text.CONTEXT_ACK)
        self.assertEqual(current["content"], "[11-02 05:00] 真实用户消息")
        self.assertNotIn(":00:00", context["content"])
        self.assertNotIn("-0500", context["content"])
        self.store.finish(record, content="正常回复", finish_reason="stop", now=self.now)
        request, next_round = self.start("第二句")
        source = self.store.history_input(next_round)
        self.assertNotIn(chat_text.CONTEXT_ACK, str(source["history"]))
        empty = self.store.save_context(next_round, request, source, {"status": "empty", "cards": []})
        self.assertEqual(empty["messages"][-3]["role"], "user")
        self.assertIn("当前时间：", empty["messages"][-3]["content"])
        self.assertNotIn("retrieved_memories", empty["messages"][-3]["content"])
        self.assertFalse(any(m["role"] == "system" for m in empty["messages"]))

    def test_tool_annotations_include_each_name_and_return_presence(self):
        from memory_core.conversations.conversation_history import render_turn
        tools = []
        for index, (name, status) in enumerate((("天气", "success"), ("联网搜索", "failure"),
                ("查日历", "unknown"), ("读文件", None))):
            tools.append(dict(kind="call", tool_call_id=str(index), tool_name=name))
            if status:
                tools.append(dict(kind="result", tool_call_id=str(index), reported_status=status))
        rendered = render_turn({"generation_state": "complete"}, [], tools, self.settings)
        self.assertEqual(rendered, [{"role": "assistant", "content": "[本轮调用了工具：天气（已返回）、联网搜索（已返回）、查日历（已返回）、读文件（未返回）]"}])

    def test_explicit_unused_receipt_is_saved_without_incrementing_usage(self):
        mid = self.store.write_card("合成卡片", [1, 0, 0])
        request, record = self.start("带记忆问题")
        self.snapshot(request, record, [dict(memory_id=mid, number=1, content="合成卡片")])
        self.store.finish(record, content="正常回复", finish_reason="stop", reported_none=True, now=self.now)
        with connect(self.app) as c:
            receipt = c.execute("SELECT usage_receipt FROM memory_core.conversation_requests WHERE id=%s", (record["request_id"],)).fetchone()["usage_receipt"]
            self.assertEqual(receipt, dict(status="reported_unused", reported_ids=[], counted_ids=[], counted=False))
            self.assertEqual(c.execute("SELECT used_count FROM memory_core.memory_items WHERE id=%s", (mid,)).fetchone()["used_count"], 0)

    def test_receipt_deduplicates_tools_retries_and_rejects_uninjected_ids(self):
        mid = self.store.write_card("合成卡片", [1, 0, 0])
        other = ConversationStore(self.app, "two").write_card("别的空间", [1, 0, 0])
        cards = [dict(memory_id=mid, number=1, content="合成卡片")]
        request, record = self.start("带记忆问题")
        self.snapshot(request, record, cards)
        self.store.finish(
            record,
            content="半段",
            state="interrupted",
            reported=[1, 1, 2, other],
            now=self.now,
        )
        request, retry = self.start("带记忆问题", intent={"operation": "retry"})
        self.snapshot(request, retry, cards)
        self.store.finish(
            retry, content="完整正文", finish_reason="stop", reported=[1], now=self.now
        )
        self.assertFalse(
            self.store.finish(
                retry,
                content="重复提交",
                finish_reason="stop",
                reported=[1],
                now=self.now,
            )
        )
        with connect(self.app) as c:
            card = c.execute(
                "SELECT used_count,last_used_at,created_at,updated_at FROM memory_core.memory_items WHERE id=%s",
                (mid,),
            ).fetchone()
            self.assertEqual(card["used_count"], 1)
            self.assertEqual(card["updated_at"], card["created_at"])
            self.assertEqual(
                c.execute(
                    "SELECT used_count FROM memory_core.memory_items WHERE id=%s",
                    (other,),
                ).fetchone()["used_count"],
                0,
            )
            receipts = c.execute(
                "SELECT usage_receipt FROM memory_core.conversation_requests ORDER BY id"
            ).fetchall()
            self.assertEqual(receipts[1]["usage_receipt"]["reported_ids"], [mid])
            self.assertEqual(receipts[1]["usage_receipt"]["counted_ids"], [])

    def test_persona_and_frontend_system_fallback_no_old_history_frozen(self):
        request = body(
            "当前",
            [
                {"role": "system", "content": "前端系统"},
                {"role": "developer", "content": "前端世界书"},
            ],
        )
        request["messages"][2:2] = [
            {"role": "user", "content": "不采纳前端历史"},
            {"role": "assistant", "content": "不采纳前端回复"},
        ]
        record = self.store.begin(request, {}, now=self.now)
        upstream = self.snapshot(request, record)
        self.assertEqual(
            [m["content"] for m in upstream["messages"][:2]], ["前端系统", "前端世界书"]
        )
        self.assertNotIn("不采纳", str(upstream))
        self.store.finish(record, content="回复", finish_reason="stop", now=self.now)
        self.config.set_persona("one", expected_revision=1, persona="后端人设")
        request["messages"][-1]["content"] = "下一句"
        record = self.store.begin(request, {}, now=self.now)
        upstream = self.snapshot(request, record)
        self.assertEqual(upstream["messages"][0]["content"], "后端人设")
        self.assertNotIn("前端系统", str(upstream))
        self.assertNotIn("前端世界书", str(upstream))

    def test_frozen_tool_replay_identical_bytes_and_images_from_current_request(self):
        request, first = self.start(
            [
                {"type": "text", "text": "看图查一下"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,FIRST_IMAGE",
                        "detail": "low",
                    },
                },
            ]
        )
        initial = self.snapshot(request, first)
        call = {
            "id": "image-tool",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        self.store.finish(
            first,
            content="先查询",
            tools=[call],
            finish_reason="tool_calls",
            now=self.now,
        )
        continuation = deepcopy(request)
        continuation["messages"][-1]["content"][1]["image_url"][
            "url"
        ] = "data:image/png;base64,SECOND_IMAGE"
        continuation["messages"] += [
            {"role": "assistant", "content": "先查询", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "image-tool", "content": "结果"},
        ]
        record = self.store.begin(continuation, {}, now=self.now + timedelta(minutes=1))
        self.assertEqual(
            initial["messages"][:-1], record["upstream_body"]["messages"][:-3]
        )
        self.assertIn("SECOND_IMAGE", str(record["upstream_body"]))
        with connect(self.app) as c:
            frozen = c.execute(
                "SELECT frozen_context FROM memory_core.conversation_turns WHERE id=%s",
                (first["turn_id"],),
            ).fetchone()["frozen_context"]
            self.assertNotIn("FIRST_IMAGE", str(frozen))
            self.assertNotIn("SECOND_IMAGE", str(frozen))
