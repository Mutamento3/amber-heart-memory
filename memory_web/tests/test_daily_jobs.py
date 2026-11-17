import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from memory_core.storage.clock import business_day_bounds, format_timestamp
from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore
from memory_core.daily.daily_policy import DailySettings
from memory_core.daily.daily_tasks import DailyTasks
from memory_core.storage.db import connect
from memory_core.daily.diaries import DiaryStore
from memory_core.storage.install import install
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.daily.summaries import SummaryStore
from memory_core.daily.summary_policy import SummarySettings
from memory_core.tests.support import DatabaseTestCase
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.jobs.daily_jobs import DailyJobs
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.diary_model import SyntheticDiaryModel
from memory_web.testing.fake_upstream import FakeUpstream

ORIGIN = "https://memory.example.test"


class DailyJobTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        self.core = Settings(3)
        install(self.owner, self.core)
        install_web(self.owner)
        for space in ("one", "two"):
            SpaceConfiguration(self.owner).configure(
                space, Participant("测试用户"), Participant("测试助手")
            )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        password = Path(temp.name) / "password.hash"
        write_password(password, "synthetic password")
        self.settings = WebSettings(self.app, self.owner, ORIGIN, password)
        self.now = datetime.now(timezone.utc)
        self.model = SyntheticDiaryModel()
        self.addCleanup(self.model.release.set)
        self.jobs = DailyJobs(
            self.settings, self.core, model=self.model, clock=lambda: self.now
        )
        self.day = self.jobs.policy.diary_range(self.now, self.core)[1]
        self.store = ConversationStore(self.app, "one")
        self.diaries = DiaryStore(self.app, "one")

    def tearDown(self):
        pass

    def seed(self, day=None, *, tools=False):
        day = day or self.day
        then = business_day_bounds(day, self.core)[0] + timedelta(hours=1)
        body = dict(model="synthetic", messages=[dict(role="user", content="合成问题")])
        record = self.store.begin(body, {"operation": "new"}, now=then)
        if tools:
            call = dict(
                id="call-1",
                type="function",
                function=dict(name="合成工具", arguments="tool_arguments"),
            )
            self.store.finish(
                record,
                content="调用前正文",
                tools=[call],
                finish_reason="tool_calls",
                now=then,
            )
            body["messages"] += [
                dict(role="assistant", content="调用前正文", tool_calls=[call]),
                dict(role="tool", tool_call_id="call-1", content="tool_result"),
            ]
            record = self.store.begin(body, {}, now=then)
        self.store.finish(record, content="合成回应", finish_reason="stop", now=then)
        return record

    def tick(self, jobs=None):
        jobs = jobs or self.jobs

        async def run():
            await jobs.tick()
            await jobs.drain()

        asyncio.run(run())

    def test_catch_up_bound_empty_day_skip_and_restart_do_not_duplicate(self):
        self.jobs.policy = DailySettings(backfill_days=2)
        self.seed(self.day - timedelta(days=4))
        self.seed(self.day - timedelta(days=1))
        self.tick()
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNone(self.diaries.get((self.day - timedelta(days=4)).isoformat()))
        self.assertIsNotNone(
            self.diaries.get((self.day - timedelta(days=1)).isoformat())
        )
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT state FROM memory_core.daily_tasks WHERE task_kind='diary' AND business_date=%s",
                    (self.day.isoformat(),),
                ).fetchone()["state"],
                "skipped",
            )
        restarted = DailyJobs(
            self.settings,
            self.core,
            model=SyntheticDiaryModel(),
            clock=lambda: self.now,
        )
        restarted.policy = self.jobs.policy
        self.tick(restarted)
        self.assertEqual(restarted.model.calls, [])
        self.assertEqual(restarted.work("one", self.now), [])

    def test_failure_retries_after_deadline_and_user_edit_is_never_overwritten(self):
        self.seed()
        self.model.fail = True
        self.tick()
        self.assertIsNone(self.diaries.get(self.day.isoformat()))
        self.now += timedelta(minutes=4)
        self.tick()
        self.assertEqual(len(self.model.calls), 1)
        self.now += timedelta(minutes=1)
        self.model.fail = False
        self.tick()
        diary = self.diaries.get(self.day.isoformat())
        self.assertIsNotNone(diary)
        self.diaries.edit(diary["id"], "用户保留的版本", 1, now=self.now)
        self.tick(
            DailyJobs(
                self.settings,
                self.core,
                model=SyntheticDiaryModel(),
                clock=lambda: self.now,
            )
        )
        self.assertEqual(
            self.diaries.get(self.day.isoformat())["content"], "用户保留的版本"
        )

    def test_two_workers_only_one_model_call_and_chat_space_lock_remains_free(self):
        self.seed()
        self.model.release.clear()
        other = DailyJobs(
            self.settings,
            self.core,
            model=SyntheticDiaryModel(),
            clock=lambda: self.now,
        )

        async def run():
            await self.jobs.tick()
            for attempt in range(200):
                if self.model.started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.model.started.is_set())
            await other.tick()
            await other.drain()
            self.assertEqual(other.model.calls, [])
            # A blocked diary model must not hold the chat's space row lock.
            record = await asyncio.to_thread(
                self.store.begin,
                dict(
                    model="synthetic",
                    messages=[dict(role="user", content="今天的新消息")],
                ),
                {"operation": "new"},
                now=self.now,
            )
            await asyncio.to_thread(
                self.store.finish,
                record,
                content="正常回应",
                finish_reason="stop",
                now=self.now,
            )
            self.model.release.set()
            await self.jobs.drain()

        try:
            asyncio.run(run())
        finally:
            self.model.release.set()
        self.assertEqual(len(self.model.calls), 1)

    def test_daily_cleanup_clears_raw_tools_summaries_and_material_copies_but_keeps_diary(
        self,
    ):
        old = self.day - timedelta(days=40)
        record = self.seed(old, tools=True)
        self.seed(old)
        summary = SummaryStore(
            self.app, "one", SummarySettings(trigger_messages=4, retain_turns=1)
        )
        plan = summary.next_plan(old.isoformat())
        then = business_day_bounds(old, self.core)[1] + timedelta(hours=1)
        summary.save(plan, dict(content="合成摘要", model="synthetic"), now=then)
        tasks = DailyTasks(self.app, "one")
        task = tasks.claim("diary", old.isoformat(), now=then)
        self.diaries.publish(
            self.diaries.plan(old.isoformat(), now=then),
            dict(content="保留的日记", model="synthetic"),
            task,
            now=then,
        )
        with connect(self.app) as c:
            message = c.execute(
                "SELECT id FROM memory_core.conversation_messages WHERE turn_id=%s AND role='user'",
                (record["turn_id"],),
            ).fetchone()["id"]
            c.execute(
                """INSERT INTO memory_core.source_materials(bot_id,source_key,content_sha256,business_date,source_kind,segments,created_at)
                VALUES ('one','synthetic-copy',%s,%s,'mainline',%s,%s)""",
                (
                    "a" * 64,
                    old.isoformat(),
                    Jsonb(
                        [
                            dict(
                                segment_id="conversation-message:" + str(message),
                                text="合成副本",
                            )
                        ]
                    ),
                    format_timestamp(then, self.core),
                ),
            )
        self.tick()
        with connect(self.app) as c:
            for table, column in (
                ("conversation_messages", "content_parts"),
                ("conversation_tool_events", "payload"),
                ("conversation_summaries", "content"),
                ("source_materials", "segments"),
            ):
                self.assertTrue(
                    all(
                        row[column] is None
                        for row in c.execute(
                            f"SELECT {column} FROM memory_core.{table}"
                        )
                    )
                )
        self.assertEqual(self.diaries.get(old.isoformat())["content"], "保留的日记")

    def test_diary_endpoints_require_admin_csrf_space_and_current_revision(self):
        self.seed()
        self.tick()
        with TestClient(
            create_app(
                self.settings, diary_models=self.model, daily_clock=lambda: self.now
            ),
            base_url=ORIGIN,
        ) as http:
            self.assertEqual(http.get("/api/spaces/one/diaries").status_code, 401)
            csrf = http.post(
                "/api/login",
                headers={"Origin": ORIGIN},
                json={"password": "synthetic password"},
            ).json()["csrf"]
            listing = http.get("/api/spaces/one/diaries").json()
            identifier = listing["items"][0]["id"]
            path = "/api/spaces/one/diaries/" + str(identifier)
            self.assertEqual(
                http.get("/api/spaces/two/diaries/" + str(identifier)).status_code, 400
            )
            body = dict(content="网页编辑的日记", expected_revision=1)
            self.assertEqual(
                http.patch(path, headers={"Origin": ORIGIN}, json=body).status_code, 403
            )
            headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
            saved = http.patch(path, headers=headers, json=body)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertTrue(saved.json()["user_edited"])
            edited = http.get("/api/spaces/one/diaries").json()["items"][0]
            self.assertEqual(edited["excerpt"], "网页编辑的日记")
            self.assertNotIn("content", edited)
            self.assertEqual(
                http.patch(path, headers=headers, json=body).status_code, 409
            )

    def test_default_diary_http_adapter_uses_only_fake_upstream_and_hides_keys(self):
        self.seed()
        with closing(FakeUpstream()) as fake:
            fake.reply = "合成日记描述这一天的安排。" * 10
            settings = replace(
                self.settings,
                access_key_master=Fernet.generate_key().decode(),
                upstream_base_url=fake.url,
                upstream_api_key="synthetic-diary-key",
                diary_model="synthetic-diary-model",
            )
            with TestClient(
                create_app(settings, daily_clock=lambda: self.now), base_url=ORIGIN
            ) as http:
                http.portal.call(http.app.state.daily.tick)
                http.portal.call(http.app.state.daily.drain)
            requests = [
                r
                for r in fake.requests
                if r.get("body", {}).get("model") == "synthetic-diary-model"
            ]
            self.assertEqual(len(requests), 1)
            self.assertIn("测试助手", requests[0]["body"]["messages"][0]["content"])
        self.assertEqual(self.diaries.get(self.day.isoformat())["content"], fake.reply)
        self.assertNotIn(
            "synthetic-diary-key", str(self.diaries.get(self.day.isoformat()))
        )
