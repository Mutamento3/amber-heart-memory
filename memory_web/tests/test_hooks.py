from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from memory_core.conversations.chat_text import RECALL_END, RECEIPT_RULE
from memory_core.conversations.conversations import ConversationStore
from memory_core.conversations.hook_turns import HookTurns, PENDING_LIFETIME
from memory_core.daily.receipt_judge import ReceiptJudgeStore
from memory_core.storage.clock import business_day_bounds, business_day_label
from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.testing.images import image_url
from memory_core.testing.provider_server import ProviderServer
from memory_core.tests.support import DatabaseTestCase
from memory_web.admin.access_keys import AccessKeys
from memory_web.admin.password import write_password
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.install import install_web
from memory_web.testing.diary_model import SyntheticDiaryModel
from memory_web.testing.extraction_model import SyntheticExtractionHTTP
from memory_web.testing.fake_upstream import FakeUpstream
from memory_web.testing.recall_models import SyntheticRecallModels
from memory_web.testing.summary_model import SyntheticSummaryModel
from memory_web.tests.test_image_descriptions import ControlledVision
from memory_web.tests.test_receipt_judge_daily import StubJudge


ORIGIN = "https://memory.example.test"
EVENTS = {
    "session-start": "SessionStart",
    "user-prompt-submit": "UserPromptSubmit",
    "stop": "Stop",
}


class HookMigrationTests(DatabaseTestCase):
    def test_upgrade_preserves_preexisting_proxy_turns(self):
        self.addCleanup(super().tearDown)
        core = Settings(3)
        install(self.owner, core, target_version=30)
        SpaceConfiguration(self.owner).configure("one", Participant("读者"), Participant("助手"))
        body = {"model": "synthetic", "messages": [{"role": "user", "content": "代理原话"}]}
        store = ConversationStore(self.app, "one")
        old = store.begin(body, {"operation": "new"})
        store.finish(old, content="代理回复", finish_reason="stop")
        self.assertTrue(install(self.owner, core))
        self.assertFalse(install(self.owner, core))
        with connect(self.app) as c:
            row = c.execute(
                """SELECT hook_session,hook_prompt,history_state FROM memory_core.conversation_turns
                WHERE id=%s""", (old["turn_id"],)
            ).fetchone()
        self.assertEqual(row, {"hook_session": None, "hook_prompt": None, "history_state": "current"})

    def tearDown(self):
        pass


class HookTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        self.core = Settings(3)
        install(self.owner, self.core)
        install_web(self.owner)
        self.configuration = SpaceConfiguration(self.owner)
        for space in ("one", "two"):
            self.configuration.configure(space, Participant("读者"), Participant("助手"))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        password = Path(folder.name) / "password.hash"
        write_password(password, "synthetic admin")
        master = Fernet.generate_key().decode()
        self.keys = AccessKeys(self.app, self.owner, master)
        self.auth = self.key_for("one")
        self.fake = FakeUpstream()
        self.addCleanup(self.fake.close)
        self.vision = ControlledVision()
        self.diary = SyntheticDiaryModel()
        self.summary = SyntheticSummaryModel()
        self.judge = StubJudge()
        self.extraction = SyntheticExtractionHTTP()
        provider = ProviderServer()
        self.addCleanup(provider.close)
        provider.hook = self.extraction.respond
        self.today = business_day_label(datetime.now(timezone.utc), self.core)
        self.clock = business_day_bounds(self.today, self.core)[0] + timedelta(minutes=1)
        env = patch.dict("os.environ", {
            "MEMORY_SUMMARY_TRIGGER_MESSAGES": "4",
            "MEMORY_SUMMARY_RETAIN_TURNS": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        self.settings = WebSettings(
            self.app, self.owner, ORIGIN, password,
            access_key_master=master, upstream_base_url=self.fake.url, upstream_api_key="synthetic-key",
            extraction_model="synthetic-extractor",
            extraction_base_url=provider.url, extraction_api_key="synthetic-key",
        )
        self.models = SyntheticRecallModels()
        self.app_http = create_app(
            self.settings, recall_models=self.models, image_models=self.vision,
            diary_models=self.diary, summary_models=self.summary, receipt_judge_models=self.judge,
            daily_clock=lambda: self.clock,
        )
        self.http = TestClient(self.app_http, base_url=ORIGIN)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.addCleanup(self.vision.release.set)
        self.store = ConversationStore(self.app, "one")

    def tearDown(self):
        pass

    def key_for(self, space):
        return {"Authorization": "Bearer " + self.keys.rotate(space)["key"]}

    def post(self, path, body, *, auth=None, status=200):
        response = self.http.post(
            "/hooks/" + path, json=body, headers=self.auth if auth is None else auth,
        )
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def claude(self, path, *, session="session-a", prompt_id=None, **fields):
        output = self.post(path, dict(
            hook_event_name=EVENTS[path], session_id=session,
            cwd="/synthetic/path", transcript_path="/synthetic/transcript",
            **({"prompt_id": prompt_id} if prompt_id else {}),
            **fields,
        ))
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], EVENTS[path])
        return output["hookSpecificOutput"]["additionalContext"]

    def prompt(self, turn_id, message="合成对话：约好一起读书", **fields):
        return self.post("user-prompt-submit", dict(turn_id=turn_id, message=message, **fields))

    def stop(self, turn_id, reply, **fields):
        return self.post("stop", dict(turn_id=turn_id, reply=reply, **fields))

    def test_session_context_contains_four_sections_and_never_persona(self):
        self.configuration.set_persona("one", expected_revision=1, persona="这段人设留在空间")
        self.configuration.set_core_memory("one", expected_revision=2, core_memory="合成核心记忆")
        with connect(self.app) as c:
            c.execute(
                """INSERT INTO memory_core.conversation_diaries
                (bot_id,business_date,content,source_revision,source_digest,source_turn_ids,model)
                VALUES ('one',%s,'已有的近日日记',0,%s,ARRAY[1],'synthetic-diary')""",
                ((self.today - timedelta(days=1)).isoformat(), "a" * 64),
            )
            c.execute("INSERT INTO memory_core.recent_notes (bot_id,health) VALUES ('one','整理后的书签')")
        self.prompt(1)
        self.stop(1, "上一轮回复")
        self.prompt(2)
        self.stop(2, "第二轮回复")
        self.http.portal.call(self.app_http.state.proxy.summaries.drain)
        result = self.post("session-start", {})
        for field, value in {
            "core_memory": "合成核心记忆", "diary": "已有的近日日记",
            "recent_notes": "整理后的书签", "summary": "合成摘要1",
        }.items():
            self.assertIn(value, result[field])
            self.assertIn(value, result["context"])
        self.assertNotIn("这段人设留在空间", str(result))
        self.assertEqual(self.claude("session-start", source="startup"), result["context"])
        self.stop(1, "替换已经被摘要覆盖的回复")
        self.http.portal.call(self.app_http.state.proxy.summaries.drain)
        result = self.post("session-start", {})
        self.assertIn("合成摘要2", result["summary"])
        self.assertNotIn("合成摘要1", result["summary"])
        self.assertIn("替换已经被摘要覆盖的回复", self.summary.calls[-1][1])
        self.configuration.set_core_memory("one", expected_revision=3, core_memory="🌙" * 5000)
        clipped = self.claude("session-start")
        self.assertLessEqual(len(clipped.encode("utf-16-le")) // 2, 10_000)
        self.assertGreater(len(clipped), 4_000)

    def test_proxy_recall_parity_and_daily_consumers_use_latest_reply(self):
        card = self.store.write_card("合成卡片正文", [1, 0, 0], title="合成标题", source_date="2026-06-01")
        value = self.prompt(1)
        self.assertEqual(value["cards"], [dict(
            memory_id=card, number=1, title="合成标题", content="合成卡片正文",
            source_date="2026-06-01", item_kind="atomic", experience_mode=None,
        )])
        self.assertEqual(self.claude("user-prompt-submit", prompt_id="cc-prompt", prompt="合成对话：约好一起读书"), value["context"])
        self.assertNotIn("<used>", value["context"])
        # Same approved card and history inputs on the same space.
        with connect(self.app) as c:
            self.assertEqual(c.execute(
                """SELECT count(*) AS n FROM memory_core.conversation_turns
                WHERE bot_id='one' AND history_state='current'""",
            ).fetchone()["n"], 0)
        output = self.http.post("/v1/chat/completions", json={
            "model": "synthetic-model", "messages": [{"role": "user", "content": "合成对话：约好一起读书"}],
            "memory": {"operation": "new"},
        }, headers=self.auth)
        self.assertEqual(output.status_code, 200, output.text)
        supplement = self.fake.requests[-1]["body"]["messages"][-3]["content"]
        start = supplement.index("<retrieved_memories>")
        end = supplement.index(RECALL_END, start) + len(RECALL_END)
        proxy_recall = supplement[start:end]
        self.assertIn("<used>", proxy_recall)
        self.assertEqual(proxy_recall.replace("\n" + RECEIPT_RULE, "", 1), value["context"])

        first = self.stop(1, "旧版回复。")
        newest = self.stop(1, "合成最新回复。\n<used>1</used>")
        self.assertTrue(newest["replaced"])
        self.assertNotEqual(newest["turn_id"], first["turn_id"])
        self.assertEqual(self.stop(1, "合成最新回复。"), dict(turn_id=newest["turn_id"], replaced=False))
        self.assertEqual(self.prompt(1), value)
        self.http.portal.call(self.app_http.state.proxy.drain)

        day = self.today.isoformat()
        judge = ReceiptJudgeStore(self.app, "one")
        candidates = {item["turn_id"]: item for item in judge.candidates(day)}
        self.assertNotIn(first["turn_id"], candidates)
        self.assertEqual(candidates[newest["turn_id"]]["cards"][0]["memory_id"], card)
        self.assertEqual(candidates[newest["turn_id"]]["reply"], "合成最新回复。")

        self.clock = business_day_bounds(self.today, self.core)[1] + timedelta(hours=1)
        self.http.portal.call(self.app_http.state.daily.run_space, "one", self.clock)
        with connect(self.app) as c:
            jobs = {
                row["task_kind"]: row for row in c.execute(
                    """SELECT task_kind,state,result FROM memory_core.daily_tasks
                    WHERE bot_id='one' AND business_date=%s""", (day,)
                ).fetchall()
            }
            self.assertEqual(jobs["diary"]["state"], "done")
            self.assertEqual(jobs["extraction"]["state"], "done")
            self.assertEqual(jobs["receipts"]["state"], "done")
            count = c.execute("SELECT used_count FROM memory_core.memory_items WHERE id=%s", (card,)).fetchone()
            self.assertGreater(count["used_count"], 0)
        self.assertIn("合成最新回复。", self.diary.calls[-1][1])
        self.assertNotIn("旧版回复。", self.diary.calls[-1][1])
        self.assertIn("合成卡片正文", str(self.judge.calls))

    def test_hook_uses_recall_deadline_and_runs_without_chat_upstream(self):
        self.store.write_card("合成卡片", [1, 0, 0])
        self.assertEqual(self.settings.recall_timeout_seconds, 8)
        settings = replace(
            self.settings, upstream_base_url=None, upstream_api_key=None, recall_timeout_seconds=0.05,
        )
        models = SyntheticRecallModels(delay=0.2)
        app = create_app(settings, recall_models=models, daily_clock=lambda: self.clock)
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.post("/hooks/user-prompt-submit", headers=self.auth, json={
                "turn_id": 1, "message": "很长的合成问题所以清理岗位也会进入总时限的计算范围",
            })
            self.assertEqual(response.json(), {"context": "", "cards": []})
            response = client.post("/hooks/stop", headers=self.auth, json={"turn_id": 1, "reply": "模型回复"})
            self.assertEqual(response.status_code, 200, response.text)
        with connect(self.app) as c:
            snapshot = c.execute("SELECT recall_snapshot FROM memory_core.conversation_turns").fetchone()
        self.assertEqual(snapshot["recall_snapshot"]["status"], "timeout")

    def test_admin_deletes_hook_position_and_replay_cannot_recreate_it(self):
        self.prompt(1, message="合成已删除的问题")
        first = self.stop(1, "合成旧回复")
        latest = self.stop(1, "合成替换回复")
        self.assertTrue(latest["replaced"])
        response = self.http.post("/api/login", headers={"Origin": ORIGIN}, json={"password": "synthetic admin"})
        headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]}
        listing = self.http.get("/api/spaces/one/conversations").json()
        self.assertEqual(listing["items"][0]["id"], latest["turn_id"])
        response = self.http.post("/api/spaces/one/conversations/delete",
                                  headers=headers, json={"position_ids": [listing["items"][0]["position_id"]]})
        self.assertEqual(response.json(), {"deleted": 1})
        self.assertEqual(self.http.get("/api/spaces/one/conversations").json()["items"], [])
        self.post("stop", {"turn_id": 1, "reply": "迟到的替换回复"}, status=404)
        self.post("user-prompt-submit", {"turn_id": 1, "message": "合成已删除的问题"}, status=409)
        with connect(self.app) as c:
            rows = c.execute("SELECT id,content_deleted_at FROM memory_core.conversation_turns ORDER BY id").fetchall()
        self.assertEqual([row["id"] for row in rows], [first["turn_id"], latest["turn_id"]])
        self.assertTrue(all(row["content_deleted_at"] for row in rows))

    def test_claude_pins_prompt_id_and_legacy_sessions_use_latest_prompt(self):
        self.claude("user-prompt-submit", prompt_id="first", prompt="有编号的第一句")
        self.claude("user-prompt-submit", prompt_id="second", prompt="有编号的第二句")
        self.assertEqual(self.claude("stop", prompt_id="first", last_assistant_message="第一句的回复"), "")
        self.claude("stop", prompt_id="second", last_assistant_message="第二句的回复")
        self.claude("user-prompt-submit", session="legacy", prompt="没配对的消息")
        self.claude("user-prompt-submit", session="legacy", prompt="旧版最新消息")
        self.claude("stop", session="other", last_assistant_message="错误会话")
        self.claude("stop", session="legacy", last_assistant_message="旧版回复")
        with connect(self.app) as c:
            pairs = c.execute(
                """SELECT array_agg(m.content_parts->0->>'text' ORDER BY m.event_index) AS pair
                FROM memory_core.conversation_turns t JOIN memory_core.conversation_messages m ON t.id=m.turn_id
                WHERE t.bot_id='one' AND t.history_state='current'
                GROUP BY t.id ORDER BY t.position_id"""
            ).fetchall()
        self.assertEqual([row["pair"] for row in pairs], [
            ["有编号的第一句", "第一句的回复"], ["有编号的第二句", "第二句的回复"],
            ["旧版最新消息", "旧版回复"],
        ])

    def test_claude_recall_records_only_context_that_fits(self):
        cards = [
            self.store.write_card("🌙" * 7000, [1, 0, 0], title=f"长卡 {number}")
            for number in range(2)
        ]
        context = self.claude("user-prompt-submit", prompt_id="long", prompt="回想长卡")
        self.assertLessEqual(len(context.encode("utf-16-le")) // 2, 10_000)
        self.assertTrue(context.endswith("</retrieved_memories>"))
        self.claude("stop", prompt_id="long", last_assistant_message="看到了第一张卡")
        [candidate] = ReceiptJudgeStore(self.app, "one").candidates(self.today.isoformat())
        self.assertEqual(len(candidate["cards"]), 1)
        self.assertIn(candidate["cards"][0]["memory_id"], cards)
        self.assertIn(candidate["cards"][0]["content"], context)

    def test_images_are_described_in_background_even_after_replacement(self):
        self.vision.release.clear()
        url = image_url()
        self.prompt(1, "", images=[{"url": url}])
        self.assertTrue(self.vision.started.wait(2))
        first = self.stop(1, "回复一")
        newest = self.stop(1, "回复二")
        self.vision.release.set()
        self.http.portal.call(self.app_http.state.proxy.drain)
        self.assertEqual(len(self.vision.calls), 1)
        with connect(self.app) as c:
            rows = c.execute(
                """SELECT turn_id,content_parts,image_descriptions FROM memory_core.conversation_messages
                WHERE turn_id=ANY(%s) AND role='user' ORDER BY turn_id""",
                ([first["turn_id"], newest["turn_id"]],),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["content_parts"], [{"type": "image_placeholder"}])
            self.assertIn("合成图像的客观描述", str(row["image_descriptions"]))
            self.assertNotIn(url, str(row))

    def test_space_auth_validation_conflicts_and_expiry(self):
        for path, payload in (
            ("session-start", {}), ("user-prompt-submit", {"turn_id": 1, "message": "问"}),
            ("stop", {"turn_id": 1, "reply": "答"}),
        ):
            self.post(path, payload, auth={}, status=401)
            self.post(path, payload, auth={"Authorization": "Bearer invalid"}, status=401)
        self.prompt(1, conversation_id="first")
        self.post("stop", {"turn_id": 1, "reply": "答"}, status=404)
        self.post("stop", {"turn_id": 1, "reply": "答", "conversation_id": "first"}, auth=self.key_for("two"), status=404)
        self.post("user-prompt-submit", {"turn_id": 1, "message": "另一句", "conversation_id": "first"}, status=409)
        self.post("user-prompt-submit", {"turn_id": True, "message": "问"}, status=400)
        self.post("session-start", {"hook_event_name": "Stop", "session_id": "cc"}, status=400)
        HookTurns.expire_pending(self.app, self.core, datetime.now(timezone.utc) + PENDING_LIFETIME)
        self.post("stop", {"turn_id": 1, "reply": "答", "conversation_id": "first"}, status=404)
        with connect(self.app) as c:
            row = c.execute(
                """SELECT m.content_parts,t.recall_snapshot,t.history_state
                FROM memory_core.conversation_turns t JOIN memory_core.conversation_messages m ON t.id=m.turn_id
                WHERE t.bot_id='one'"""
            ).fetchone()
        self.assertIsNone(row["content_parts"])
        self.assertEqual(row["recall_snapshot"], {"status": "expired", "cards": []})
        self.assertEqual(row["history_state"], "candidate")
        self.keys.revoke("one")
        self.post("session-start", {}, status=401)
