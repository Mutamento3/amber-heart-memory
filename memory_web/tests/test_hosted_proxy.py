from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from threading import Event
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import psycopg

from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.storage.space import SpaceConfiguration, Participant
from memory_core.tests.support import DatabaseTestCase
from memory_web.admin.access_keys import AccessKeys
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.fake_upstream import FakeUpstream
from memory_web.testing.recall_models import SyntheticRecallModels

ORIGIN = "https://memory.example.test"


class HostedProxyTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner, Settings(3))
        install_web(self.owner)
        self.config = SpaceConfiguration(self.owner)
        for space in ("one", "two"):
            self.config.configure(space, Participant("用户"), Participant("助手"))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password = Path(temporary.name) / "password.hash"
        write_password(password, "synthetic admin")
        master = Fernet.generate_key().decode()
        self.keys = AccessKeys(self.app, self.owner, master)
        self.auth = {"Authorization": "Bearer " + self.keys.rotate("one")["key"]}
        self.fake = FakeUpstream()
        self.addCleanup(self.fake.close)
        self.models = SyntheticRecallModels()
        self.settings = WebSettings(
            self.app,
            self.owner,
            ORIGIN,
            password,
            access_key_master=master,
            upstream_base_url=self.fake.url,
            upstream_api_key="synthetic-upstream",
        )
        self.application = create_app(self.settings, recall_models=self.models)
        self.http = TestClient(self.application, base_url=ORIGIN)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.store = ConversationStore(self.app, "one")

    def tearDown(self):
        pass

    def chat(
        self, value="合成问题", *, model="synthetic-model", messages=None, **extra
    ):
        body = dict(
            model=model,
            messages=messages or [{"role": "user", "content": value}],
            **extra,
        )
        response = self.http.post("/v1/chat/completions", headers=self.auth, json=body)
        self.http.portal.call(self.application.state.proxy.drain)
        return response

    def cards(self, count=1):
        return [
            self.store.write_card(
                "合成记忆正文" + str(i), [1, 0, 0], title="合成记忆" + str(i)
            )
            for i in range(count)
        ]

    def login(self):
        csrf = self.http.post(
            "/api/login",
            headers={"Origin": ORIGIN},
            json={"password": "synthetic admin"},
        ).json()["csrf"]
        return {"Origin": ORIGIN, "X-CSRF-Token": csrf}

    def test_upstream_only_hosted_history_no_frontend_history_and_persona(self):
        self.chat("真实时间线的第一句")
        self.config.set_persona("one", expected_revision=1, persona="后端中性人设")
        response = self.chat(
            messages=[
                {"role": "system", "content": "旧系统世界书"},
                {"role": "user", "content": "未导入的历史"},
                {"role": "assistant", "content": "伪造的旧回答"},
                {"role": "user", "content": "本轮文字"},
            ]
        )
        self.assertEqual(response.status_code, 200, response.text)
        upstream = self.fake.requests[-1]["body"]
        self.assertEqual(upstream["messages"][0]["content"], "后端中性人设")
        self.assertFalse(any(m["role"] in ("system", "developer") for m in upstream["messages"][1:]))
        self.assertEqual([m["role"] for m in upstream["messages"][-3:]], ["user", "assistant", "user"])
        self.assertIn("当前时间：", upstream["messages"][-3]["content"])
        self.assertEqual(upstream["messages"][-2]["content"], "（以上是这轮的补充上下文，我会参考它来回应。）")
        for missing in (
            "旧系统世界书",
            "未导入的历史",
            "伪造的旧回答",
            "reasoning_content",
        ):
            self.assertNotIn(missing, str(upstream))
        self.assertIn("真实时间线的第一句", str(upstream))
        self.assertIn("本轮文字", upstream["messages"][-1]["content"])

    def test_recall_real_pipeline_snapshot_only_injected_numbers_counted_and_match_clean_body(
        self,
    ):
        mid = self.cards()[0]
        self.fake.reply = "干净回答\n<used>1,1,5,999</used>\n<used>1</used>"
        first = self.chat("记忆问题", memory={"request_id": "receipt-first"})
        clean = first.json()["choices"][0]["message"]["content"]
        self.assertEqual(clean, "干净回答")
        with connect(self.app) as c:
            turn = c.execute("SELECT * FROM memory_core.conversation_turns").fetchone()
            self.assertEqual(turn["recall_snapshot"]["cards"][0]["memory_id"], mid)
            self.assertEqual(turn["context_manifest"]["recall_ids"], [mid])
            self.assertEqual(
                c.execute(
                    "SELECT used_count FROM memory_core.memory_items WHERE id=%s",
                    (mid,),
                ).fetchone()["used_count"],
                1,
            )
            self.assertNotIn(
                "<used>",
                str(
                    c.execute(
                        "SELECT content_parts FROM memory_core.conversation_messages"
                    ).fetchall()
                ),
            )
        self.fake.reply = "下一句回答"
        next_response = self.chat(
            messages=[
                {"role": "assistant", "content": clean},
                {"role": "user", "content": "下一句"},
            ]
        )
        with connect(self.app) as c:
            new = c.execute(
                "SELECT * FROM memory_core.conversation_turns ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNone(new["replaces_turn_id"])
            self.assertIsNotNone(new["anchor_message_id"])
        before = len(self.fake.requests)
        duplicate = self.chat("记忆问题", memory={"request_id": "receipt-first"})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(len(self.fake.requests), before)
        self.assertEqual(next_response.status_code, 200)

    def test_stream_split_truncated_duplicate_reports_and_visible_archive_match(self):
        mid = self.cards()[0]
        for index, (parts, expected, count) in enumerate(
            [
                (
                    ["第一", "段\n<us", "ed>1,", "1,99</u", "sed>\n<used>1</used>"],
                    "第一段",
                    1,
                ),
                (["第一段\n<us"], "第一段", 0),
                (["第一段\n<used>1</us"], "第一段", 0),
            ]
        ):
            self.fake.stream_parts = parts
            self.fake.stream_finish = "stop" if index == 0 else None
            response = self.chat(
                "流式" + str(index), stream=True, memory={"operation": "new"}
            )
            data = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            visible = "".join(
                choice.get("delta", {}).get("content", "")
                for event in data
                for choice in event.get("choices", [])
            )
            self.assertEqual(visible, expected)
            self.assertNotIn("<us", response.text.lower())
            with connect(self.app) as c:
                request = c.execute(
                    "SELECT * FROM memory_core.conversation_requests ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self.assertEqual(
                    request["usage_receipt"]["reported_ids"], [mid] if count else []
                )
                saved = c.execute(
                    "SELECT content_parts FROM memory_core.conversation_messages WHERE request_id=%s AND role='assistant'",
                    (request["id"],),
                ).fetchone()
                self.assertEqual(
                    saved["content_parts"], [{"type": "text", "text": expected}]
                )

    def test_recalled_topic_summary_includes_its_public_source_tool_id(self):
        child = self.cards()[0]
        parent = self.store.write_card(
            "合成主题总结正文", [1, 0, 0], title="合成主题总结", item_kind="topic_summary"
        )
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET parent_reflection_id=%s WHERE id=%s",
                      (parent, child))
        response = self.chat("植物")
        self.assertEqual(response.status_code, 200, response.text)
        context = self.fake.requests[-1]["body"]["messages"][-3]["content"]
        self.assertIn(f'合成主题总结正文\n（这是主题总结 M-{parent}；想看来源记忆，可调用 memory：'
                      f'{{"action":"get_sources","memory_id":"M-{parent}"}}）', context)
        self.assertIn("没用上就写 <used>无</used>", context)

    def test_recall_degrades_each_stage_and_total_timeout(self):
        self.cards(7)
        for failure in ("rerank", "select"):
            self.models.fail = {failure}
            response = self.chat("精排退路" + failure, memory={"operation": "new"})
            self.assertEqual(response.status_code, 200, response.text)
            with connect(self.app) as c:
                snapshot = c.execute(
                    "SELECT recall_snapshot FROM memory_core.conversation_turns ORDER BY id DESC LIMIT 1"
                ).fetchone()["recall_snapshot"]
                self.assertEqual(snapshot["status"], "degraded")
                self.assertEqual(len(snapshot["cards"]), 5)
        self.application.state.proxy.context.settings = replace(
            self.settings, recall_timeout_seconds=0.05
        )
        self.models.delay = 0.2
        before = time.monotonic()
        response = self.chat(
            "很长的合成问题所以清理岗位也会进入总时限的计算范围",
            memory={"operation": "new"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(time.monotonic() - before, 1)
        with connect(self.app) as c:
            snapshot = c.execute(
                "SELECT recall_snapshot FROM memory_core.conversation_turns ORDER BY id DESC LIMIT 1"
            ).fetchone()["recall_snapshot"]
            self.assertEqual(snapshot["status"], "timeout")
            self.assertEqual(snapshot["cards"], [])
        upstream = self.fake.requests[-1]["body"]
        self.assertNotIn("<retrieved_memories>", str(upstream))
        self.assertIn("当前时间：", upstream["messages"][-3]["content"])
        self.assertEqual(upstream["messages"][-3]["role"], "user")
        self.login()
        counts = self.http.get("/api/spaces/one/overview").json()["recall_counts"]
        self.assertEqual(counts["timed_out"], 1)
        self.assertEqual(counts["degraded"], 2)

    def test_total_timeout_injects_the_last_completed_order(self):
        self.cards(7)
        self.application.state.proxy.context.settings = replace(
            self.settings, recall_timeout_seconds=0.75
        )
        for stage in ("rerank", "select"):
            with self.subTest(stage=stage):
                release = Event()
                entered = []

                def blocked(query, ids, **kwargs):
                    entered.append(ids)
                    release.wait(3)

                try:
                    with patch.object(self.models, stage, blocked):
                        response = self.chat("植物", memory={"operation": "new"})
                finally:
                    release.set()
                self.assertEqual(response.status_code, 200, response.text)
                with connect(self.app) as c:
                    snapshot = c.execute(
                        "SELECT recall_snapshot FROM memory_core.conversation_turns ORDER BY id DESC LIMIT 1"
                    ).fetchone()["recall_snapshot"]
                self.assertEqual(snapshot["status"], "timeout")
                self.assertEqual([card["memory_id"] for card in snapshot["cards"]], entered[0][:5])
                self.assertIn("<retrieved_memories>", self.fake.requests[-1]["body"]["messages"][-3]["content"])

    def test_single_document_stage_timeout_still_runs_final_selection(self):
        self.cards(7)
        self.application.state.proxy.context.settings = replace(
            self.settings, recall_models=replace(self.settings.recall_models, timeout_seconds=0.1)
        )
        release = Event()
        ordering_input = []

        def blocked(query, ids, **kwargs):
            ordering_input.extend(ids)
            release.wait(3)
            return list(reversed(range(len(ids))))

        try:
            with patch.object(self.models, "rerank", blocked):
                response = self.chat("植物")
        finally:
            release.set()
        self.assertEqual(response.status_code, 200, response.text)
        with connect(self.app) as c:
            snapshot = c.execute(
                "SELECT recall_snapshot FROM memory_core.conversation_turns ORDER BY id DESC LIMIT 1"
            ).fetchone()["recall_snapshot"]
        self.assertEqual(snapshot["status"], "degraded")
        self.assertIn(("select", ordering_input), self.models.calls)
        self.assertEqual([card["memory_id"] for card in snapshot["cards"]], ordering_input[:5])

    def test_unconfigured_models_use_existing_keyword_fallback_and_expose_failure(self):
        with TestClient(create_app(self.settings), base_url=ORIGIN) as ordinary:
            response = ordinary.post(
                "/v1/chat/completions",
                headers=self.auth,
                json=dict(
                    model="synthetic-model",
                    messages=[{"role": "user", "content": "没有记忆卡片"}],
                ),
            )
            self.assertEqual(response.status_code, 200)
        with connect(self.app) as c:
            snapshot = c.execute(
                "SELECT recall_snapshot FROM memory_core.conversation_turns"
            ).fetchone()["recall_snapshot"]
            self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(self.models.calls, [])

    def test_pure_image_skips_recall_and_continuation_freezes_every_prefix_byte(self):
        image = {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,ONLY_CURRENT_IMAGE",
                "detail": "high",
            },
        }
        messages = [
            {"role": "system", "content": "稳定的人设"},
            {"role": "user", "content": [image]},
        ]
        first = self.chat(model="synthetic-round", messages=messages)
        self.assertEqual(first.status_code, 200, first.text)
        initial = deepcopy(self.fake.requests[-1]["body"])
        self.assertEqual(self.models.calls, [])
        self.config.set_persona(
            "one", expected_revision=1, persona="下一轮才生效的人设"
        )
        messages += [
            first.json()["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call-0", "content": "第一结果"},
        ]
        second = self.chat(model="synthetic-round", messages=messages)
        self.assertEqual(second.status_code, 200, second.text)
        replay = self.fake.requests[-1]["body"]
        encoded = lambda v: json.dumps(
            v, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertEqual(encoded(initial["messages"]), encoded(replay["messages"][:-2]))
        self.assertEqual(initial["model"], replay["model"])
        with connect(self.app) as c:
            turn = c.execute(
                "SELECT frozen_context,recall_snapshot FROM memory_core.conversation_turns"
            ).fetchone()
            self.assertNotIn("ONLY_CURRENT_IMAGE", str(turn))
            self.assertEqual(turn["recall_snapshot"]["status"], "skipped_image")

    def test_tool_recall_once_and_cards_prefix_remain_frozen_after_edit(self):
        mid = self.cards()[0]
        self.fake.reply = "工具阶段的正文\n<used>1</used>"
        messages = [{"role": "user", "content": "查一下记忆"}]
        first = self.chat(model="synthetic-round", messages=messages)
        initial = deepcopy(self.fake.requests[-1]["body"]["messages"])
        calls = len(self.models.calls)
        self.config.configure("one", Participant("下一轮才采用的名字"), Participant("助手"), expected_revision=1)
        messages += [
            first.json()["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call-0", "content": "第一结果"},
        ]
        with connect(self.app) as c:
            c.execute(
                "UPDATE memory_core.memory_items SET content='工具期间改变的卡片'"
            )
        second = self.chat(model="synthetic-round", messages=messages)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(self.models.calls), calls)
        replay = self.fake.requests[-1]["body"]["messages"]
        self.assertEqual(initial, replay[:-2])
        self.assertNotIn("工具期间改变的卡片", str(replay))
        self.assertNotIn("下一轮才采用的名字", str(replay))
        self.assertNotIn("<used>", second.text)
        messages += [
            second.json()["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call-1", "content": "第二结果"},
        ]
        third = self.chat(model="synthetic-round", messages=messages, stream=True)
        self.assertEqual(third.status_code, 200, third.text)
        self.assertNotIn("<used>", third.text)
        self.assertEqual(len(self.models.calls), calls)
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT used_count FROM memory_core.memory_items WHERE id=%s",
                    (mid,),
                ).fetchone()["used_count"],
                1,
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_requests WHERE usage_receipt->>'status'='reported'"
                ).fetchone()["n"],
                3,
            )

    def test_tool_round_restores_reasoning_the_client_dropped(self):
        # DeepSeek thinking mode needs each tool-call message's reasoning back;
        # some clients drop it, so the proxy restores it from memory only.
        self.cards()
        self.fake.round_reasoning = "工具前思考"
        messages = [{"role": "user", "content": "查一下记忆"}]
        first = self.chat(model="synthetic-round", messages=messages)
        dropped = dict(first.json()["choices"][0]["message"])
        self.assertEqual(dropped.pop("reasoning_content"), "工具前思考0")
        messages += [dropped, {"role": "tool", "tool_call_id": "call-0", "content": "第一结果"}]
        second = self.chat(model="synthetic-round", messages=messages)
        self.assertEqual(second.status_code, 200, second.text)
        replay = self.fake.requests[-1]["body"]["messages"]
        self.assertEqual([m.get("reasoning_content") for m in replay if m.get("tool_calls")], ["工具前思考0"])
        kept = dict(second.json()["choices"][0]["message"], reasoning_content="客户端自己带的")
        messages += [kept, {"role": "tool", "tool_call_id": "call-1", "content": "第二结果"}]
        third = self.chat(model="synthetic-round", messages=messages, stream=True)
        self.assertEqual(third.status_code, 200, third.text)
        replay = self.fake.requests[-1]["body"]["messages"]
        self.assertEqual([m.get("reasoning_content") for m in replay if m.get("tool_calls")], ["工具前思考0", "客户端自己带的"])
        with connect(self.app) as c:
            self.assertNotIn("工具前思考", str(c.execute("SELECT * FROM memory_core.conversation_requests").fetchall()))

    def test_explicit_unused_and_unreported_remain_distinct_in_both_protocols(self):
        mid = self.cards()[0]
        for stream in (False, True):
            for tag, status in (("<used>无</used>", "reported_unused"),
                                ("<used>无</used>\n<used> 无 </used>", "reported_unused"),
                                ("", "unreported"), ("<used>99</used>", "unreported"),
                                ("<used>无</us", "unreported")):
                with self.subTest(stream=stream, tag=tag):
                    raw = "正常回复" + ("\n" + tag if tag else "")
                    self.fake.reply = raw
                    self.fake.stream_parts = list(raw) if stream else None
                    response = self.chat("核对回执", stream=stream, memory={"operation": "new"})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertNotIn("<us", response.text.lower())
                    with connect(self.app) as c:
                        receipt = c.execute("SELECT usage_receipt FROM memory_core.conversation_requests ORDER BY id DESC LIMIT 1").fetchone()["usage_receipt"]
                        self.assertEqual(receipt["status"], status)
                        self.assertEqual(receipt["counted_ids"], [])
        with connect(self.app) as c:
            self.assertEqual(c.execute("SELECT used_count FROM memory_core.memory_items WHERE id=%s", (mid,)).fetchone()["used_count"], 0)
        self.login()
        listing = self.http.get('/api/spaces/one/conversations').json()
        statuses = {r['usage_receipt']['status'] for turn in listing['items'] for r in turn['requests']}
        self.assertEqual(statuses, {'reported_unused', 'unreported'})

    def test_conversation_listing_scrolls_back_by_cursor_and_hides_expired_turns(self):
        for text in ("第一轮", "第二轮", "第三轮", "第四轮"):
            self.fake.reply = text + "的回复"
            self.assertEqual(self.chat(text).status_code, 200)
        with connect(self.app) as c:
            oldest = c.execute("SELECT id FROM memory_core.conversation_turns ORDER BY position_id LIMIT 1").fetchone()["id"]
            c.execute("UPDATE memory_core.conversation_turns SET content_deleted_at=created_at WHERE id=%s", (oldest,))
        self.login()
        texts = lambda page: [turn["messages"][0]["display_text"] for turn in page["items"]]
        newest = self.http.get("/api/spaces/one/conversations?size=2").json()
        self.assertEqual(texts(newest), ["第三轮", "第四轮"])
        self.assertTrue(newest["has_more"])
        self.assertEqual(newest["total"], 4)
        older = self.http.get(f"/api/spaces/one/conversations?size=2&before={newest['next_before']}").json()
        self.assertEqual(texts(older), ["第二轮"])
        self.assertFalse(older["has_more"])

    def test_client_history_after_deleting_middle_latest_and_regenerating(self):
        prefix = []

        def send(question, reply, *, remember=True):
            self.fake.reply = reply
            user = {"role": "user", "content": question}
            response = self.chat(messages=[*prefix, user])
            self.assertEqual(response.status_code, 200, response.text)
            if remember:
                prefix.extend((user, {"role": "assistant", "content": reply}))
            return int(response.headers["x-memory-turn-id"])

        send("留下的第一轮", "留下的第一轮回复")
        middle = send("删除的中间轮", "删除的中间回复")
        send("留下的第三轮", "留下的第三轮回复")
        headers = self.login()
        endpoint = "/api/spaces/one/conversations/delete"
        response = self.http.post(endpoint, headers=headers, json={"position_ids": [middle]})
        self.assertEqual(response.json(), {"deleted": 1})
        tail = send("删除的最后轮", "删除的最后回复")
        sent = self.fake.requests[-1]["body"]["messages"]
        self.assertIn("留下的第一轮", str(sent))
        self.assertIn("留下的第三轮", str(sent))
        self.assertNotIn("删除的中间", str(sent))
        response = self.http.post(endpoint, headers=headers, json={"position_ids": [tail]})
        self.assertEqual(response.json(), {"deleted": 1})

        # Regenerating a removed prompt itself cannot send or archive it.
        before = len(self.fake.requests)
        retry = self.chat(messages=prefix[:-1])
        self.assertEqual(retry.status_code, 400, retry.text)
        self.assertIn("这轮聊天已删除", retry.text)
        self.assertEqual(len(self.fake.requests), before)
        resumed = send("继续的聊天", "第一次继续的回复", remember=False)
        regenerated = send("继续的聊天", "重新生成的回答", remember=False)
        self.assertNotEqual(resumed, regenerated)
        for request in self.fake.requests[-2:]:
            self.assertNotIn("删除的最后", str(request["body"]))
            self.assertNotIn("删除的中间", str(request["body"]))
            self.assertIn("留下的第三轮", str(request["body"]))
        listing = self.http.get("/api/spaces/one/conversations").json()["items"]
        self.assertEqual(listing[-1]["id"], regenerated)
        self.assertEqual(listing[-1]["position_id"], resumed)
        self.assertEqual(len(listing), 3)
        with connect(self.app) as c:
            messages = c.execute("SELECT content_parts FROM memory_core.conversation_messages").fetchall()
        self.assertNotIn("删除的最后", str(messages))
        self.assertNotIn("删除的中间", str(messages))

    def test_delete_requires_session_csrf_and_all_positions_in_this_space(self):
        other = ConversationStore(self.app, "two")
        body = {"model": "synthetic", "messages": [{"role": "user", "content": "另一空间"}]}
        foreign = other.begin(body, {})
        other.finish(foreign, content="别的空间", finish_reason="stop")
        local = int(self.chat("当前空间").headers["x-memory-turn-id"])
        endpoint = "/api/spaces/one/conversations/delete"
        payload = {"position_ids": [local]}
        self.assertEqual(self.http.post(endpoint, headers={"Origin": ORIGIN, **self.auth}, json=payload).status_code, 401)
        headers = self.login()
        self.assertEqual(self.http.post(endpoint, headers={"Origin": ORIGIN}, json=payload).status_code, 403)
        mixed = {"position_ids": [local, foreign["turn_id"]]}
        self.assertEqual(self.http.post(endpoint, headers=headers, json=mixed).status_code, 404)
        self.assertEqual(self.http.get("/api/spaces/one/conversations").json()["items"][0]["id"], local)
        self.assertEqual(self.http.get("/api/spaces/two/conversations").json()["items"][0]["id"], foreign["turn_id"])

    def test_delete_refuses_history_frozen_in_a_tool_turn(self):
        local = int(self.chat("正在使用的原话").headers["x-memory-turn-id"])
        waiting = self.chat("请调用工具", model="synthetic-round")
        self.assertEqual(waiting.status_code, 200)
        waiting_id = int(waiting.headers["x-memory-turn-id"])
        headers = self.login()
        endpoint = "/api/spaces/one/conversations/delete"
        response = self.http.post(endpoint, headers=headers, json={"position_ids": [local]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("当前回复正在使用所选聊天", response.text)
        listing = self.http.get("/api/spaces/one/conversations").json()["items"]
        self.assertEqual(listing[0]["id"], local)
        self.assertEqual(listing[-1]["generation_state"], "waiting_tools")

        response = self.http.post(endpoint, headers=headers, json={"position_ids": [waiting_id]})
        self.assertEqual(response.json(), {"deleted": 1})
        with connect(self.app) as c:
            turn = c.execute("""SELECT generation_state,frozen_context,content_deleted_at
                FROM memory_core.conversation_turns WHERE id=%s""", (waiting_id,)).fetchone()
            state = c.execute("""SELECT active_turn_id,active_request_id,lease_token,lease_expires_at
                FROM memory_core.conversation_space_state WHERE bot_id='one'""").fetchone()
        self.assertEqual(turn["generation_state"], "incomplete")
        self.assertIsNone(turn["frozen_context"])
        self.assertIsNotNone(turn["content_deleted_at"])
        self.assertTrue(all(value is None for value in state.values()))
        self.assertEqual(self.chat("继续下一句").status_code, 200)

    def test_history_failure_blocks_upstream_but_recall_failure_does_not(self):
        with patch.object(
            ConversationStore,
            "history_input",
            side_effect=psycopg.OperationalError("synthetic"),
        ):
            response = self.chat()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "history_unavailable")
        self.assertEqual(self.fake.requests, [])
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_messages"
                ).fetchone()["n"],
                1,
            )

    def test_nonchat_remains_exact_passthrough_and_does_not_recall_or_lock(self):
        messages = [
            {"role": "system", "content": "辅助系统"},
            {"role": "user", "content": "辅助请求"},
        ]
        self.fake.reply = "辅助原样 <used>1</used>"
        response = self.chat(messages=messages, memory={"operation": "non_chat"})
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"], self.fake.reply
        )
        self.assertEqual(self.fake.requests[-1]["body"]["messages"], messages)
        self.assertEqual(self.models.calls, [])
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_turns"
                ).fetchone()["n"],
                0,
            )

    def test_kelivo_and_operit_background_requests_pass_through_without_storing(self):
        for messages in (
            [{"role": "system", "content": "\n你要从对话中构建长期记忆图谱。\n只输出 JSON。"},
             {"role": "user", "content": "问题：\n合成问题\n\n解决方案：\n合成回答"}],
            [{"role": "system", "content": "You are an AI assistant responsible for generating a conversation summary. Keep it short."},
             {"role": "user", "content": "合成对话"}],
            [{"role": "system", "content": '你是\\"角色卡生成助手\\"。请严格按照以下流程进行角色卡生成：'},
             {"role": "user", "content": "合成角色"}],
            [{"role": "user", "content": "I will give you some dialogue content in the <dialogue> block. 合成对话"}],
            [{"role": "system", "content": "你负责生成简短的对话标题。\n根据用户第一条消息总结。"},
             {"role": "user", "content": "用户第一条消息：\n合成对话"}],
            [{"role": "system", "content": "You generate short conversation titles.\nSummarize."},
             {"role": "user", "content": [{"type": "text", "text": "First user message: 合成"}]}],
        ):
            self.fake.reply = "合成标题"
            response = self.chat(messages=messages)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "合成标题")
            self.assertEqual(self.fake.requests[-1]["body"]["messages"], messages)
        self.assertEqual(self.models.calls, [])
        with connect(self.app) as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM memory_core.conversation_turns").fetchone()["n"], 0)

    def test_admin_persona_csrf_conflict_timeline_and_recall_only_current_space(self):
        path = "/api/spaces/one/persona"
        self.assertEqual(
            self.http.patch(
                path,
                headers=self.auth,
                json={"expected_revision": 1, "persona": "越权"},
            ).status_code,
            403,
        )
        headers = self.login()
        response = self.http.patch(
            path, headers=headers, json={"expected_revision": 1, "persona": "中性人设"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], 2)
        self.assertEqual(
            self.http.patch(
                path,
                headers=headers,
                json={"expected_revision": 1, "persona": "陈旧编辑"},
            ).status_code,
            409,
        )
        self.cards()
        self.fake.reply = "有回执\n<used>1</used>"
        first = self.chat("同一个问题")
        second = self.chat("同一个问题")
        first_id = first.headers["x-memory-turn-id"]
        second_id = second.headers["x-memory-turn-id"]
        listing = self.http.get("/api/spaces/one/conversations").json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["id"], int(second_id))
        self.assertNotIn("<used>", json.dumps(listing))
        self.assertEqual(
            self.http.get(
                f"/api/spaces/one/conversations/{first_id}/recall"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.http.get(
                f"/api/spaces/two/conversations/{second_id}/recall"
            ).status_code,
            400,
        )
        detail = self.http.get(
            f"/api/spaces/one/conversations/{second_id}/recall"
        ).json()
        self.assertEqual(detail["requests"][0]["usage_receipt"]["status"], "reported")
        self.assertEqual(len(detail["recall_snapshot"]["cards"]), 1)
        self.assertEqual(
            self.http.request(
                "DELETE", "/api/spaces/one/conversations", headers=headers, json={}
            ).status_code,
            405,
        )
