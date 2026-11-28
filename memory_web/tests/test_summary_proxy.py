from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from memory_core.bookmarks.recent_notes import RecentNotesStore
from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.daily.summaries import active_summaries
from memory_core.daily.summary_policy import SummarySettings
from memory_core.storage.install import install
from memory_core.storage.space import SpaceConfiguration, Participant
from memory_core.tests.support import DatabaseTestCase
from memory_web.admin.access_keys import AccessKeys
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.fake_upstream import FakeUpstream
from memory_web.testing.summary_model import SyntheticSummaryModel

ORIGIN = "https://memory.example.test"


class SummaryProxyTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        env = patch.dict(
            "os.environ",
            {
                "MEMORY_SUMMARY_TRIGGER_MESSAGES": "4",
                "MEMORY_SUMMARY_RETAIN_TURNS": "1",
                "MEMORY_SUMMARY_ROLLUP_TRIGGER": "8",
                "MEMORY_SUMMARY_MAX_HISTORY_CHARS": "30000",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        install(self.owner, Settings(3))
        install_web(self.owner)
        for space in ("one", "two"):
            SpaceConfiguration(self.owner).configure(
                space, Participant("用户"), Participant("助手")
            )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        password = Path(tmp.name) / "password.hash"
        write_password(password, "synthetic admin")
        master = Fernet.generate_key().decode()
        self.keys = AccessKeys(self.app, self.owner, master)
        self.auth = {"Authorization": "Bearer " + self.keys.rotate("one")["key"]}
        self.fake = FakeUpstream()
        self.addCleanup(self.fake.close)
        self.summary = SyntheticSummaryModel()
        self.addCleanup(self.summary.release.set)
        self.settings = WebSettings(
            self.app,
            self.owner,
            ORIGIN,
            password,
            access_key_master=master,
            upstream_base_url=self.fake.url,
            upstream_api_key="synthetic-upstream",
        )
        self.application = create_app(self.settings, summary_models=self.summary)
        self.http = TestClient(self.application, base_url=ORIGIN)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.addCleanup(self.summary.release.set)

    def tearDown(self):
        pass

    def request(
        self, question, *, stream=False, model="synthetic-model", messages=None
    ):
        return self.http.post(
            "/v1/chat/completions",
            headers=self.auth,
            json=dict(
                model=model,
                stream=stream,
                messages=messages or [{"role": "user", "content": question}],
                memory={"operation": "new"},
            ),
        )

    def chat(self, question, **extra):
        response = self.request(question, **extra)
        self.http.portal.call(self.application.state.proxy.drain)
        return response

    def test_summaries_run_after_reply_and_stable_prefix_is_at_the_beginning(self):
        self.chat("第一句")
        self.assertEqual(self.summary.calls, [])
        self.chat("第二句", stream=True)
        self.assertEqual(len(self.summary.calls), 1)
        self.chat("第三句")
        sent = self.fake.requests[-1]["body"]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("合成摘要1", sent[0]["content"])
        self.assertNotIn("第一句", str(sent[1:]))
        self.assertIn("第二句", str(sent))
        self.assertNotIn("tool_call", str(self.summary.calls))

    def test_slow_background_summary_does_not_delay_nonstream_reply_or_duplicate_work(
        self,
    ):
        self.chat("先聊一句")
        self.summary.release.clear()
        timer = threading.Timer(5, self.summary.release.set)
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        response = self.request("触发摘要")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(self.summary.started.wait(2))
        other = SyntheticSummaryModel()
        with TestClient(
            create_app(self.settings, summary_models=other), base_url=ORIGIN
        ) as worker:
            response = worker.post(
                "/v1/chat/completions",
                headers=self.auth,
                json=dict(
                    model="synthetic-model",
                    messages=[{"role": "user", "content": "另一端新一句"}],
                    memory={"operation": "new"},
                ),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(other.calls, [])
            self.summary.release.set()
            timer.cancel()
            worker.portal.call(worker.app.state.proxy.drain)
            self.assertEqual(other.calls, [])
        self.summary.release.set()
        timer.cancel()
        self.http.portal.call(self.application.state.proxy.drain)
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summaries"
                ).fetchone()["n"],
                2,
            )

    def test_failure_preserves_raw_and_next_finished_round_retries(self):
        self.summary.fail = True
        self.chat("第一句")
        self.chat("第二句")
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summary_turns"
                ).fetchone()["n"],
                0,
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_messages"
                ).fetchone()["n"],
                4,
            )
        self.summary.fail = False
        self.chat("第三句")
        self.assertGreaterEqual(len(self.summary.calls), 2)
        with connect(self.app) as c:
            self.assertGreater(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summary_turns"
                ).fetchone()["n"],
                0,
            )

    def test_status_and_manual_retry_preserve_reply_progress_across_workers(self):
        self.summary.fail = True
        first = self.chat("第一句")
        second = self.chat("第二句")
        first_id = int(first.headers["x-memory-turn-id"])
        second_id = int(second.headers["x-memory-turn-id"])
        path = "/api/spaces/one/summaries"
        self.assertEqual(self.http.get(path + "/status", headers=self.auth).status_code, 401)
        model = SyntheticSummaryModel()
        model.release.clear()
        with TestClient(create_app(self.settings, summary_models=model), base_url=ORIGIN) as worker:
            try:
                csrf = worker.post("/api/login", headers={"Origin": ORIGIN},
                                   json={"password": "synthetic admin"}).json()["csrf"]
                failed = worker.get(path + "/status").json()
                self.assertEqual((failed["status"], failed["cycle_count"], failed["trigger_count"]),
                                 ("failed", 4, 4))
                self.assertEqual(failed["attempt"]["turn_id"], second_id)
                attempt = {"attempt_id": failed["attempt"]["id"]}
                headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
                self.assertEqual(worker.post(path + "/retry", json=attempt).status_code, 403)
                self.assertEqual(worker.post(path + "/retry", headers=headers,
                                             json={"attempt_id": "0" * 32}).json()["status"], "failed")
                self.assertEqual(worker.post(path + "/retry", headers=headers, json=attempt).json()["status"],
                                 "generating")
                self.assertTrue(model.started.wait(2))
                self.assertEqual(worker.get(path + "/status").json()["status"], "generating")
                self.assertEqual(worker.post(path + "/retry", headers=headers, json=attempt).json()["status"],
                                 "generating")
            finally:
                model.release.set()
            worker.portal.call(worker.app.state.proxy.drain)
            self.assertEqual(len(model.calls), 1)
            final = worker.get(path + "/status").json()
            self.assertEqual((final["status"], final["cycle_count"], final["covered_turn_ids"]),
                             ("success", 2, [first_id]))
            turns = worker.get("/api/spaces/one/conversations").json()["items"]
            self.assertEqual([(turn["summary_progress"]["cycle_count"], turn["summarized"]) for turn in turns],
                             [(2, True), (4, False)])
            self.assertEqual(turns[-1]["summary_progress"]["status"], "success")
            self.assertEqual(worker.get("/api/spaces/two/summaries/status").json()["status"], "idle")

    def test_tool_continuation_replays_frozen_raw_even_after_summary_commits(self):
        self.chat("第一句")
        self.summary.release.clear()
        timer = threading.Timer(5, self.summary.release.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.request("第二句")
        self.assertTrue(self.summary.started.wait(2))
        messages = [{"role": "user", "content": "请查一下"}]
        first = self.request("", model="synthetic-round", messages=messages)
        initial = deepcopy(self.fake.requests[-1]["body"]["messages"])
        self.assertNotIn("instant_summaries", str(initial))
        self.summary.release.set()
        timer.cancel()
        self.http.portal.call(self.application.state.proxy.drain)
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summaries"
                ).fetchone()["n"],
                1,
            )
        messages += [
            first.json()["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call-0", "content": "合成结果"},
        ]
        second = self.chat("", model="synthetic-round", messages=messages)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.fake.requests[-1]["body"]["messages"][:-2], initial)

    def test_claude_cache_breakpoints_replay_across_workers(self):
        config = SpaceConfiguration(self.owner)
        config.set_persona("one", expected_revision=1, persona="合成人设")
        config.set_core_memory("one", expected_revision=2, core_memory="合成核心记忆")
        RecentNotesStore(self.app, "one").save({"daily": "合成日常书签"}, 0)
        self.chat("较早的聊天")
        self.chat("留下的聊天")

        model = "anthropic/claude-sonnet-4.6"
        self.fake.tool_models.add(model)
        messages = [{"role": "user", "content": "这轮请查一下"}]
        first = self.request("", model=model, messages=messages)
        self.assertEqual(first.status_code, 200, first.text)
        initial = self.fake.requests[-1]["body"]["messages"]
        self.assertEqual(initial[0]["content"], "合成人设")
        expected = {1: "合成核心记忆", 2: "合成日常书签", 3: "合成摘要1", 5: "合成回答"}
        for i, message in enumerate(initial):
            if i in expected:
                block, = message["content"]
                self.assertEqual(block["cache_control"], {"type": "ephemeral", "ttl": "1h"})
                self.assertIn(expected[i], block["text"])
            else:
                self.assertIsInstance(message["content"], str)
        self.assertIn("留下的聊天", initial[4]["content"])
        self.assertIn("当前时间：", initial[-3]["content"])
        self.assertIn("这轮请查一下", initial[-1]["content"])
        with connect(self.app) as c:
            turn = c.execute(
                "SELECT frozen_context FROM memory_core.conversation_turns ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertNotIn("cache_control", str(turn["frozen_context"]))

        config.set_persona("one", expected_revision=3, persona="下一轮的新人设")
        messages += [
            first.json()["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call-0", "content": "合成结果"},
        ]
        with TestClient(create_app(self.settings, summary_models=self.summary), base_url=ORIGIN) as worker:
            second = worker.post(
                "/v1/chat/completions", headers=self.auth,
                json=dict(model=model, messages=messages, stream=True),
            )
            self.assertEqual(second.status_code, 200, second.text)
            worker.portal.call(worker.app.state.proxy.drain)
        replay = self.fake.requests[-1]["body"]["messages"]
        self.assertEqual(replay[:-2], initial)
        self.assertEqual(replay[-1]["content"], "合成结果")

    def test_length_protection_is_visible_upstream_and_recorded_on_dialogue_page(self):
        self.application.state.proxy.context.summary_settings = type(
            self.application.state.proxy.context.summary_settings
        )(max_history_chars=70)
        self.summary.fail = True
        self.chat("长" * 100)
        self.chat("另一句")
        sent = self.fake.requests[-1]["body"]["messages"]
        self.assertIn(
            "[更早的1轮原话这轮没有带上；不要假装看过，涉及细节时请向用户确认。]",
            str(sent),
        )
        self.assertNotIn("长" * 100, str(sent))
        csrf = self.http.post(
            "/api/login",
            headers={"Origin": ORIGIN},
            json={"password": "synthetic admin"},
        ).json()["csrf"]
        listing = self.http.get("/api/spaces/one/conversations").json()
        self.assertEqual(listing["items"][-1]["history_omitted"]["turns"], 1)

    def test_summary_api_shows_exact_coverage_and_rejects_cross_space(self):
        self.chat("第一句")
        self.chat("第二句")
        self.http.post(
            "/api/login",
            headers={"Origin": ORIGIN},
            json={"password": "synthetic admin"},
        )
        listing = self.http.get("/api/spaces/one/summaries").json()
        self.assertEqual(listing["total"], 1)
        identifier = listing["items"][0]["id"]
        detail = self.http.get("/api/spaces/one/summaries/" + str(identifier)).json()
        self.assertEqual(detail["summary"]["content"], "合成摘要1")
        self.assertEqual(len(detail["turns"]), 1)
        self.assertEqual(detail["turns"][0]["user_content"][0]["text"], "第一句")
        self.assertEqual(
            self.http.get("/api/spaces/two/summaries/" + str(identifier)).status_code,
            400,
        )

    def test_admin_summary_edit_injects_next_turn_and_is_replaced_by_delete_rebuild(self):
        self.application.state.proxy.summaries.policy = SummarySettings(
            trigger_messages=6, retain_turns=1, rollup_trigger=3
        )
        turns = []
        for i in range(1, 8):
            response = self.chat(f"合成第{i}轮原话")
            self.assertEqual(response.status_code, 200, response.text)
            turns.append(int(response.headers["x-memory-turn-id"]))
        with connect(self.app) as c:
            rollup = c.execute("SELECT id,business_date FROM memory_core.conversation_summaries WHERE level=2").fetchone()
            active = active_summaries(c, "one", rollup["business_date"])
        self.assertEqual([summary["id"] for summary in active], [rollup["id"]])
        csrf = self.http.post("/api/login", headers={"Origin": ORIGIN},
                              json={"password": "synthetic admin"}).json()["csrf"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        summaries_url = "/api/spaces/one/summaries"
        summary_url = f"{summaries_url}/{rollup['id']}"
        listed, = self.http.get(summaries_url).json()["items"]
        self.assertEqual((listed["id"], listed["revision"]), (rollup["id"], 1))
        patch = {"content": "手改后的二次摘要", "expected_revision": 1}
        self.assertEqual(self.http.patch(summary_url, json=patch).status_code, 403)
        self.assertEqual(self.http.patch(summary_url.replace("/one/", "/two/"),
                                         headers=headers, json=patch).status_code, 409)
        edited = self.http.patch(summary_url, headers=headers, json=patch)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json(), dict(id=rollup["id"], content=patch["content"], revision=2))
        self.assertEqual(self.http.get(summary_url).json()["summary"]["revision"], 2)
        self.assertEqual(self.http.patch(summary_url, headers=headers, json=patch).status_code, 409)
        self.assertEqual(self.http.patch(summary_url, headers=headers,
                                         json={"content": "  ", "expected_revision": 2}).status_code, 400)
        self.assertEqual(self.chat("修改后继续说话").status_code, 200)
        self.assertIn(patch["content"], str(self.fake.requests[-1]["body"]["messages"]))

        response = self.http.post("/api/spaces/one/conversations/delete",
                                  headers=headers,
                                  json={"position_ids": [turns[2]]})
        self.assertEqual(response.json(), {"deleted": 1})
        self.http.portal.call(self.application.state.proxy.drain)
        with connect(self.app) as c:
            expired = c.execute("SELECT content,content_deleted_at FROM memory_core.conversation_summaries WHERE id=%s",
                                (rollup["id"],)).fetchone()
            active = active_summaries(c, "one", rollup["business_date"])
        self.assertIsNone(expired["content"])
        self.assertIsNotNone(expired["content_deleted_at"])
        self.assertEqual(self.http.patch(summary_url, headers=headers,
                                         json={**patch, "expected_revision": 2}).status_code, 409)
        self.assertCountEqual([turn_id for summary in active for turn_id in summary["covered_turn_ids"]],
                              [turns[0], turns[1], turns[3], turns[4], turns[5], turns[6]])
        self.assertIn("合成第4轮原话", self.summary.calls[-1][1])
        self.assertNotIn("合成第3轮原话", self.summary.calls[-1][1])
        self.chat("删除后继续说话")
        sent = str(self.fake.requests[-1]["body"]["messages"])
        self.assertNotIn("合成第3轮原话", sent)
        self.assertNotIn(patch["content"], sent)

    def test_configured_default_http_summary_adapter_only_calls_fake_upstream(self):
        from dataclasses import replace

        settings = replace(self.settings, summary_model="synthetic-summary")
        with TestClient(create_app(settings), base_url=ORIGIN) as worker:
            for question in ("第一句", "第二句"):
                response = worker.post(
                    "/v1/chat/completions",
                    headers=self.auth,
                    json=dict(
                        model="synthetic-model",
                        messages=[{"role": "user", "content": question}],
                        memory={"operation": "new"},
                    ),
                )
                self.assertEqual(response.status_code, 200, response.text)
                worker.portal.call(worker.app.state.proxy.drain)
        summary_calls = [
            r
            for r in self.fake.requests
            if r.get("body", {}).get("model") == "synthetic-summary"
        ]
        self.assertEqual(len(summary_calls), 1)
        self.assertIn(
            "“及时摘要”记录员", summary_calls[0]["body"]["messages"][0]["content"]
        )
