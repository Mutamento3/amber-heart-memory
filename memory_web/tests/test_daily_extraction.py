import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import threading
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from memory_core.storage.clock import business_day_bounds
from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore
from memory_core.daily.daily_policy import DailySettings
from memory_core.daily.daily_tasks import DailyTasks
from memory_core.storage.db import connect
from memory_core.models.http_models import HTTPRecallModels
from memory_core.storage.install import install
from memory_core.models.model_config import ModelRole, RecallModelsConfig
from memory_core.cards.review import ReviewStore
from memory_core.cards.review_policy import ApprovalSettings
from memory_core.cards.drafts import DraftStore
from memory_core.storage.space import Participant, SpaceConfiguration, read_policy
from memory_core.testing.provider_server import ProviderServer
from memory_core.tests.support import DatabaseTestCase
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.jobs.daily_jobs import DailyJobs
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.diary_model import SyntheticDiaryModel
from memory_web.testing.extraction_model import SyntheticExtractionHTTP

ORIGIN = "https://memory.example.test"


class DailyExtractionTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        self.core = Settings(3)
        install(self.owner, self.core)
        install_web(self.owner)
        self.config = SpaceConfiguration(self.owner)
        self.config.configure("one", Participant("合成读者"), Participant("合成助手"))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        password = Path(temp.name) / "password.hash"
        write_password(password, "synthetic password")
        self.server = ProviderServer()
        self.addCleanup(self.server.close)
        self.responses = SyntheticExtractionHTTP()
        self.addCleanup(self.responses.release.set)
        self.server.hook = self.responses.respond
        roles = RecallModelsConfig(embedding=ModelRole("synthetic-embedding"))
        self.settings = WebSettings(
            self.app,
            self.owner,
            ORIGIN,
            password,
            access_key_master=Fernet.generate_key().decode(),
            upstream_base_url=self.server.url,
            upstream_api_key="synthetic-key",
            recall_models=roles,
            extraction_model="synthetic-extractor",
        )
        self.embedder = HTTPRecallModels(
            roles, 3, main_url=self.server.url, main_key="synthetic-key"
        )
        self.now = datetime.now(timezone.utc)
        self.diary = SyntheticDiaryModel()
        self.jobs = self.make_jobs()
        self.day = self.jobs.policy.extraction_range(self.now, self.core)[1]
        self.store = ConversationStore(self.app, "one")

    def tearDown(self):
        pass

    def make_jobs(self):
        return DailyJobs(
            self.settings,
            self.core,
            model=self.diary,
            embedder=self.embedder,
            embedding_model="synthetic-embedding",
            clock=lambda: self.now,
        )

    def seed(self, *, day=None, tools=False, body=None, answer="合成回应", finish=True):
        day = day or self.day
        instant = business_day_bounds(day, self.core)[0] + timedelta(hours=1)
        body = body or dict(
            model="synthetic-main",
            messages=[dict(role="user", content="合成原话：我们约好学习植物。")],
        )
        record = self.store.begin(body, {"operation": "new"}, now=instant)
        if tools:
            call = dict(
                id="tool-1",
                type="function",
                function=dict(
                    name="合成工具", arguments="TOOL_ARGUMENTS_MUST_NOT_REPLAY"
                ),
            )
            self.store.finish(
                record,
                content="合成调用前正文",
                tools=[call],
                finish_reason="tool_calls",
                now=instant,
            )
            body["messages"] += [
                dict(role="assistant", content="合成调用前正文", tool_calls=[call]),
                dict(
                    role="tool",
                    tool_call_id="tool-1",
                    content="TOOL_RESULT_MUST_NOT_REPLAY",
                ),
            ]
            record = self.store.begin(body, {}, now=instant)
        if finish:
            self.store.finish(record, content=answer, finish_reason="stop", now=instant)
        return record

    def tick(self, jobs=None):
        jobs = jobs or self.jobs

        async def run():
            await jobs.tick(now=self.now)
            await jobs.drain()

        asyncio.run(run())

    def task(self):
        with connect(self.app) as c:
            return c.execute(
                "SELECT * FROM memory_core.daily_tasks WHERE task_kind='extraction' AND business_date=%s",
                (self.day.isoformat(),),
            ).fetchone()

    def approval_task(self):
        with connect(self.app) as c:
            return c.execute("SELECT * FROM memory_core.daily_tasks WHERE task_kind='approval' ORDER BY business_date DESC LIMIT 1").fetchone()

    def advance_to_approval(self):
        with connect(self.app) as c:
            ready=c.execute("SELECT min(review_ready_at::timestamptz) AS stamp FROM memory_core.pending_memories WHERE status='pending'").fetchone()['stamp']
        self.now=ApprovalSettings().scheduled_at(ready.isoformat(),self.core,self.jobs.policy)+timedelta(seconds=1)
        self.tick()

    def test_pending_then_delayed_full_transaction_sources_usage_and_repeat(self):
        self.responses.hook = lambda stage, material: self.assertTrue(self.diary.calls)
        self.seed(tools=True)
        self.tick()
        self.assertEqual(
            [s for s, _ in self.responses.calls], ["selection", "structure"]
        )
        task = self.task()
        self.assertEqual(task["state"], "done")
        self.assertEqual(len(task["model_usage"]), 2)
        self.assertEqual(task["result"]["added_count"], 0)
        self.assertEqual(task["result"]["pending_count"], 1)
        with connect(self.app) as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM memory_core.memory_items").fetchone()["n"],0)
        self.advance_to_approval()
        with connect(self.app) as c:
            card = c.execute("SELECT * FROM memory_core.memory_items").fetchone()
            self.assertEqual(card["source_kind"], "mainline")
            self.assertEqual(card["experience_mode"], "shared")
            self.assertGreater(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.memory_keyword_tokens"
                ).fetchone()["n"],
                0,
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.memory_topics"
                ).fetchone()["n"],
                1,
            )
            self.assertEqual(
                c.execute("SELECT count(*) AS n FROM memory_core.relations").fetchone()[
                    "n"
                ],
                1,
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.memory_entities"
                ).fetchone()["n"],
                2,
            )
            material = c.execute(
                "SELECT segments FROM memory_core.source_materials"
            ).fetchone()["segments"]
            self.assertTrue(
                all(
                    s["segment_id"].startswith("conversation-message:")
                    for s in material
                )
            )
            self.assertIn("合成调用前正文", str(material))
            self.assertNotIn("TOOL_", str(material))
        self.tick(self.make_jobs())
        self.assertEqual(len(self.responses.calls), 2)
        self.assertEqual(self.approval_task()["result"]["approved"], 1)

    def test_manual_and_switch_do_not_bulk_approve_old_pending(self):
        self.config.set_auto_approval("one", expected_revision=1, enabled=False)
        self.seed()
        self.tick()
        self.assertEqual(self.task()["result"]["pending_count"], 1)
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.memory_items"
                ).fetchone()["n"],
                0,
            )
            draft = c.execute("SELECT * FROM memory_core.pending_memories").fetchone()
        self.config.set_auto_approval("one", expected_revision=2, enabled=True)
        self.tick()
        self.assertEqual(self.task()["result"]["pending_count"], 1)
        self.assertEqual(
            ReviewStore(self.app, "one").get(draft["id"])["status"], "pending"
        )

    def test_structure_failure_resumes_without_reselecting_or_half_cards(self):
        self.responses.failures["structure"] = 1
        self.seed()
        self.tick()
        self.assertEqual(self.task()["state"], "failed")
        self.assertEqual(len(self.task()["model_usage"]), 2)
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.memory_items"
                ).fetchone()["n"],
                0,
            )
        self.now += timedelta(minutes=6)
        self.tick(self.make_jobs())
        self.assertEqual(
            [s for s, _ in self.responses.calls],
            ["selection", "structure", "structure"],
        )
        self.assertEqual(self.task()["state"], "done")
        self.assertEqual(len(self.task()["model_usage"]), 3)
        self.assertEqual(self.task()["result"]["pending_count"], 1)
        self.assertNotIn("MUST_NOT_PERSIST", str(self.task()))

    def assert_http_timeout_resumes(self, slow_stage):
        completed = threading.Event()
        def slow(stage, material):
            if stage == slow_stage:
                time.sleep(0.6)
                completed.set()
        self.responses.hook = slow
        self.settings = replace(self.settings, extraction_timeout_seconds=0.25)
        self.seed()
        self.tick(self.make_jobs())
        task = self.task()
        self.assertEqual(task["state"], "failed")
        self.assertEqual([event["code"] for event in task["model_usage"][-2:]],
            ["extraction_" + slow_stage + "_timeout"] * 2)
        with connect(self.app) as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM memory_core.memory_items").fetchone()["n"], 0)
        self.assertTrue(completed.wait(2))
        self.responses.hook = None
        self.settings = replace(self.settings, extraction_timeout_seconds=2)
        self.now += timedelta(minutes=6)
        self.tick(self.make_jobs())
        self.assertEqual(self.task()["state"], "done")
        self.assertEqual(self.task()["result"]["pending_count"], 1)
        stages = [stage for stage, _ in self.responses.calls]
        expected = (["selection"] * 3 + ["structure"] if slow_stage == "selection"
            else ["selection"] + ["structure"] * 3)
        self.assertEqual(stages, expected)

    def test_selection_http_timeout_retries_without_half_cards(self):
        self.assert_http_timeout_resumes("selection")

    def test_structure_http_timeout_resumes_without_reselecting(self):
        self.assert_http_timeout_resumes("structure")

    def test_failure_in_review_rolls_back_and_retries_normal_transaction(self):
        self.seed()

        self.tick()
        with connect(self.app) as c:
            ready=c.execute("SELECT review_ready_at FROM memory_core.pending_memories").fetchone()["review_ready_at"]
        self.now=ApprovalSettings().scheduled_at(ready,self.core,self.jobs.policy)+timedelta(seconds=1)

        def fail(stage, c):
            if stage == "keywords":
                raise ValueError("synthetic write failure")

        with patch.object(ReviewStore, "_checkpoint", staticmethod(fail)):
            self.tick()
        self.assertEqual(self.approval_task()["state"], "failed")
        with connect(self.app) as c:
            for table in (
                "memory_items",
                "relations",
                "memory_entities",
                "memory_keyword_tokens",
            ):
                self.assertEqual(
                    c.execute(
                        "SELECT count(*) AS n FROM memory_core." + table
                    ).fetchone()["n"],
                    0,
                )
        self.now += timedelta(minutes=6)
        self.tick()
        self.assertEqual(self.approval_task()["result"]["approved"], 1)
        self.assertEqual(len(self.responses.calls), 2)

    def test_switch_to_manual_during_model_generation_prevents_auto_approval(self):
        self.seed()
        changed = []

        def hook(stage, material):
            if stage == "structure" and not changed:
                changed.append(True)
                self.config.set_auto_approval(
                    "one", expected_revision=1, enabled=False
                )

        self.responses.hook = hook
        self.tick()
        self.assertEqual(self.task()["state"], "failed")
        self.now += timedelta(minutes=6)
        self.tick()
        self.assertEqual(self.task()["state"], "done")
        self.assertEqual(self.task()["result"]["pending_count"], 1)
        self.assertEqual(self.task()["result"]["added_count"], 0)

    def test_missing_model_makes_no_extraction_calls_then_backfills_when_enabled(self):
        self.seed()
        unconfigured = replace(self.settings, extraction_model=None)
        jobs = DailyJobs(
            unconfigured, self.core, model=self.diary, clock=lambda: self.now
        )
        self.tick(jobs)
        self.assertIsNone(self.task())
        self.assertEqual(self.responses.calls, [])
        self.tick()
        self.assertEqual(self.task()["result"]["pending_count"], 1)

    def test_backfill_limit_and_retention_skip_without_paid_calls(self):
        self.jobs.policy = DailySettings(extraction_backfill_days=2)
        self.seed(day=self.day - timedelta(days=4))
        self.seed(day=self.day - timedelta(days=1))
        self.tick()
        self.assertEqual(
            [s for s, _ in self.responses.calls], ["selection", "structure"]
        )
        self.assertEqual(self.task()["state"], "skipped")

    def test_two_workers_share_lock_and_do_not_duplicate_extraction(self):
        self.seed()
        self.responses.block = True
        other = self.make_jobs()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.tick)
            self.assertTrue(self.responses.started.wait(4))
            second = pool.submit(self.tick, other)
            second.result(timeout=3)
            self.responses.release.set()
            first.result(timeout=10)
        self.assertEqual(len(self.responses.calls), 2)
        self.assertEqual(self.task()["result"]["pending_count"], 1)

    def test_waits_48_hours_then_next_daily_schedule_without_reextracting(self):
        self.seed()
        self.tick()
        with connect(self.app) as c:
            draft=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
        expected=ApprovalSettings().scheduled_at(draft['review_ready_at'],self.core,self.jobs.policy)
        self.now=expected-timedelta(seconds=1)
        self.tick()
        self.assertEqual(ReviewStore(self.app,'one').get(draft['id'])['status'],'pending')
        self.now=expected+timedelta(seconds=1)
        self.tick()
        self.assertEqual(ReviewStore(self.app,'one').get(draft['id'])['status'],'approved')
        self.assertEqual(len(self.responses.calls),2)
        self.tick(self.make_jobs())
        self.assertEqual(self.approval_task()['result']['approved'],1)

    def test_disabled_due_drafts_wait_then_resume_on_a_later_daily_run(self):
        self.seed()
        self.tick()
        self.config.set_auto_approval('one',expected_revision=1,enabled=False)
        self.advance_to_approval()
        with connect(self.app) as c:
            draft=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
        self.assertEqual(draft['status'],'pending')
        self.config.set_auto_approval('one',expected_revision=2,enabled=True)
        self.now+=timedelta(days=1)
        self.tick()
        self.assertEqual(ReviewStore(self.app,'one').get(draft['id'])['status'],'approved')

    def test_due_approval_accepts_a_draft_with_an_identical_vector(self):
        self.seed()
        self.tick()
        store=ReviewStore(self.app,'one')
        existing=store.write_card('合成旧卡',[1,0,0])
        with connect(self.app) as c:
            pending=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
        self.assertEqual(pending['status'],'pending')
        self.advance_to_approval()
        approved=store.get(pending['id'])
        self.assertEqual(approved['status'],'approved')
        self.assertNotEqual(approved['memory_item_id'],existing)
        self.assertIsNone(approved['dedup_state'])
        self.assertIsNone(approved['duplicate_memory_id'])
        result=self.approval_task()['result']
        self.assertEqual((result['approved'],result['duplicates']),(1,0))

    def test_human_save_restarts_waiting_period(self):
        self.seed()
        self.tick()
        workflow=ReviewStore(self.app,'one')
        with connect(self.app) as c:
            draft=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
        self.now=ApprovalSettings().scheduled_at(draft['review_ready_at'],self.core,self.jobs.policy)-timedelta(seconds=1)
        payload=draft['payload']|{'title':'人工修改后的合成标题'}
        saved=workflow.edit(draft['id'],payload,expected_version=draft['version'],expected_policy_revision=draft['policy_revision'],clock=lambda:self.now)
        self.now+=timedelta(seconds=2)
        self.tick()
        self.assertEqual(workflow.get(draft['id'])['status'],'pending')
        self.assertEqual(saved['review_ready_at'],workflow.get(draft['id'])['review_ready_at'])
        self.advance_to_approval()
        self.assertEqual(workflow.get(draft['id'])['status'],'approved')

    def test_switch_off_while_preparing_embeddings_prevents_late_ingestion(self):
        self.seed()
        self.tick()
        with connect(self.app) as c:
            draft=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
        self.now=ApprovalSettings().scheduled_at(draft['review_ready_at'],self.core,self.jobs.policy)+timedelta(seconds=1)
        original=self.embedder.embed
        switched=[]
        def embed(value):
            if not switched:
                switched.append(True)
                self.config.set_auto_approval('one',expected_revision=1,enabled=False)
            return original(value)
        with patch.object(self.embedder,'embed',side_effect=embed):
            self.tick()
        self.assertEqual(ReviewStore(self.app,'one').get(draft['id'])['status'],'pending')
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.memory_items').fetchone()['n'],0)

    def test_due_approval_runs_without_an_extraction_model(self):
        self.seed()
        self.tick()
        with connect(self.app) as c:
            ready=c.execute('SELECT review_ready_at FROM memory_core.pending_memories').fetchone()['review_ready_at']
        self.now=ApprovalSettings().scheduled_at(ready,self.core,self.jobs.policy)+timedelta(seconds=1)
        jobs=DailyJobs(replace(self.settings,extraction_model=None),self.core,model=self.diary,embedder=self.embedder,embedding_model='synthetic-embedding',clock=lambda:self.now)
        self.tick(jobs)
        self.assertEqual(self.approval_task()['result']['approved'],1)
        self.assertEqual(len(self.responses.calls),2)

    def test_late_daily_sweep_does_not_take_drafts_due_after_its_scheduled_time(self):
        from memory_web.testing.data import draft as material_draft
        from memory_core.storage.clock import format_timestamp
        candidate,_=material_draft(self.app,'one','clock-only')
        start=business_day_bounds(self.day+timedelta(days=1),self.core)[0]
        self.now=start+timedelta(hours=7)
        # Ready on business day self.day: due in the run two days later, not in a late run tomorrow.
        ready=start+timedelta(hours=1)-timedelta(days=1)
        with connect(self.app) as c:
            c.execute('UPDATE memory_core.pending_memories SET review_ready_at=%s WHERE id=%s',(format_timestamp(ready,self.core),candidate['id']))
        self.tick()
        self.assertEqual(ReviewStore(self.app,'one').get(candidate['id'])['status'],'pending')
        self.now+=timedelta(days=1)
        self.tick()
        self.assertEqual(ReviewStore(self.app,'one').get(candidate['id'])['status'],'approved')

    def test_manual_approval_does_not_get_processed_again(self):
        self.seed()
        self.tick()
        with connect(self.app) as c:
            draft=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
        expected=ApprovalSettings().scheduled_at(draft['review_ready_at'],self.core,self.jobs.policy)
        workflow=ReviewStore(self.app,'one')
        workflow.approve(draft['id'],expected_version=draft['version'],expected_policy_revision=draft['policy_revision'],request_key='human-review',embedder=self.embedder,embedding_model='synthetic-embedding')
        self.now=expected+timedelta(seconds=1)
        self.tick()
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.memory_items').fetchone()['n'],1)
        self.assertEqual(self.approval_task()['result']['approved'],0)

    def test_rejected_draft_stays_rejected_after_deadline(self):
        self.seed()
        self.tick()
        with connect(self.app) as c:
            draft=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
        expected=ApprovalSettings().scheduled_at(draft['review_ready_at'],self.core,self.jobs.policy)
        workflow=ReviewStore(self.app,'one')
        workflow.reject(draft['id'],expected_version=draft['version'])
        self.now=expected+timedelta(seconds=1)
        self.tick()
        self.assertEqual(workflow.get(draft['id'])['status'],'rejected')
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.memory_items').fetchone()['n'],0)

    def test_adopted_source_changed_during_wait_prevents_automatic_ingestion(self):
        original=self.seed()
        self.tick()
        with connect(self.app) as c:
            draft=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
        then=business_day_bounds(self.day,self.core)[0]+timedelta(hours=2)
        body=dict(model='synthetic-main',messages=[dict(role='user',content='等候期间改写的合成原话')])
        changed=self.store.begin(body,{'operation':'edit','target_turn_id':original['turn_id']},now=then)
        self.store.finish(changed,content='合成的新回复',finish_reason='stop',now=then)
        self.now=ApprovalSettings().scheduled_at(draft['review_ready_at'],self.core,self.jobs.policy)+timedelta(seconds=1)
        self.tick()
        pending=ReviewStore(self.app,'one').get(draft['id'])
        self.assertEqual((pending['status'],pending['error_code']),('pending','manual_review_required'))
        # Waiting for a person is not a failure: no five-minute retry loop.
        task=self.approval_task()
        self.assertEqual((task['state'],task['result']['manual'],task['result']['failed']),('done',[draft['id']],[]))
        self.now+=timedelta(minutes=6)
        self.tick()
        self.assertEqual(self.approval_task()['attempts'],task['attempts'])
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.memory_items').fetchone()['n'],0)

    def test_core_memory_change_leaves_due_draft_for_a_person_who_can_approve_it(self):
        self.seed()
        self.tick()
        with connect(self.app) as c:
            draft=c.execute('SELECT * FROM memory_core.pending_memories').fetchone()
            revision=read_policy(c,'one')['revision']
        # Core memory is extraction context, so the stale draft cannot be reconfirmed automatically.
        self.config.set_core_memory('one',expected_revision=revision,core_memory='合成核心记忆：喜欢植物。')
        self.now=ApprovalSettings().scheduled_at(draft['review_ready_at'],self.core,self.jobs.policy)+timedelta(seconds=1)
        self.tick()
        task=self.approval_task()
        self.assertEqual((task['state'],task['result']['manual']),('done',[draft['id']]))
        with connect(self.app) as c:
            policy=read_policy(c,'one')
            overview=self.jobs.approval_policy.overview(c,'one',policy,self.core,self.jobs.policy,self.now)
        self.assertEqual((overview['manual_count'],overview['ready_count'],overview['overdue']),(1,0,False))
        # The review page saves under the current settings first; saving clears the mark.
        saved=DraftStore(self.app,'one').edit(draft['id'],draft['payload'],expected_version=draft['version'],
            expected_policy_revision=policy['revision'])
        self.assertIsNone(saved['error_code'])
        result=ReviewStore(self.app,'one').approve(saved['id'],expected_version=saved['version'],
            expected_policy_revision=policy['revision'],request_key='human-after-core-memory',
            embedder=self.embedder,embedding_model='synthetic-embedding')
        self.assertEqual(result['status'],'approved')

    def test_two_workers_share_lock_for_due_approval(self):
        from threading import Event
        self.seed()
        self.tick()
        with connect(self.app) as c:
            ready=c.execute('SELECT review_ready_at FROM memory_core.pending_memories').fetchone()['review_ready_at']
        self.now=ApprovalSettings().scheduled_at(ready,self.core,self.jobs.policy)+timedelta(seconds=1)
        started,release=Event(),Event()
        self.addCleanup(release.set)
        original=self.embedder.embed
        def embed(value):
            started.set()
            if not release.wait(8):
                raise TimeoutError('synthetic blocking embedding')
            return original(value)
        other=self.make_jobs()
        with patch.object(self.embedder,'embed',side_effect=embed),ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.tick)
            self.assertTrue(started.wait(4))
            second=pool.submit(self.tick,other)
            second.result(timeout=3)
            release.set()
            first.result(timeout=10)
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.memory_items').fetchone()['n'],1)
        self.assertEqual(self.approval_task()['attempts'],1)

    def test_default_factory_http_configuration_overview_and_auto_approval_api(self):
        self.seed()
        app = create_app(
            self.settings, daily_clock=lambda: self.now, diary_models=self.diary
        )
        app.state.daily.enabled = False
        with TestClient(app, base_url=ORIGIN) as http:
            asyncio.run(app.state.daily.run_space("one", self.now))
            login = http.post(
                "/api/login",
                headers={"Origin": ORIGIN},
                json={"password": "synthetic password"},
            ).json()
            headers = {"Origin": ORIGIN, "X-CSRF-Token": login["csrf"]}
            overview_before = http.get("/api/spaces/one/overview").json()
            self.assertIsNotNone(overview_before["auto_approval"]["next_run_at"])
            self.assertEqual(overview_before["auto_approval"]["delay_hours"],48)
            status = overview_before["daily_extraction"]
            self.assertEqual(status["pending_count"], 1)
            self.assertTrue(status["configured"])
            self.assertEqual(status["calls"], 2)
            self.assertFalse(http.get("/api/session").json()["fixed_models"])
            response = http.patch(
                "/api/spaces/one/auto-approval",
                headers=headers,
                json=dict(expected_revision=1, auto_approve_enabled=False),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["auto_approve_enabled"])
            overview=http.get("/api/spaces/one/overview").json()
            self.assertFalse(overview["auto_approval"]["enabled"])
            self.assertEqual(
                http.patch(
                    "/api/spaces/one/auto-approval",
                    headers=headers,
                    json=dict(expected_revision=1, auto_approve_enabled=True),
                ).status_code,
                409,
            )
            self.assertEqual(
                http.patch(
                    "/api/spaces/one/auto-approval",
                    json=dict(expected_revision=2, auto_approve_enabled=True),
                ).status_code,
                403,
            )
            response = http.put(
                "/api/spaces/two/participants",
                headers=headers,
                json=dict(
                    expected_revision=0,
                    user=dict(name="第二个用户"),
                    assistant=dict(name="第二个助手"),
                ),
            )
            self.assertEqual(response.status_code, 400, response.text)

    def test_round_crossing_boundary_preserves_actual_times_and_one_day(self):
        start, end = business_day_bounds(self.day, self.core)
        before = end - timedelta(minutes=2)
        after = end + timedelta(minutes=2)
        body = dict(
            model="synthetic-main", messages=[dict(role="user", content="合成跨日原话")]
        )
        record = self.store.begin(body, {"operation": "new"}, now=before)
        self.store.finish(
            record, content="合成跨日回复", finish_reason="stop", now=after
        )
        self.tick()
        with connect(self.app) as c:
            source = c.execute("SELECT * FROM memory_core.source_materials").fetchone()
            self.assertEqual(source["business_date"], self.day.isoformat())
            stamps = [
                datetime.fromisoformat(s["timestamp"]) for s in source["segments"]
            ]
            self.assertEqual(stamps, [before, after])
            self.assertEqual(self.task()["result"]["pending_count"], 1)

    def test_images_unanswered_and_incomplete_annotations_survive_without_tool_records(
        self,
    ):
        from memory_core.conversations.chat_text import IMAGE_PLACEHOLDER, UNANSWERED, INCOMPLETE

        body = dict(
            model="synthetic-main",
            messages=[
                dict(
                    role="user",
                    content=[
                        dict(type="text", text="合成图片原话"),
                        dict(
                            type="image_url",
                            image_url=dict(
                                url="https://images.example.test/synthetic.png"
                            ),
                        ),
                    ],
                )
            ],
        )
        first = self.seed(body=body, finish=False)
        then = business_day_bounds(self.day, self.core)[0] + timedelta(hours=1)
        self.store.finish(first, state="upstream_failed", now=then)
        second = self.seed(
            finish=False,
            body=dict(
                model="synthetic-main",
                messages=[dict(role="user", content="合成中断原话")],
            ),
        )
        self.store.finish(second, content="合成半段回复", state="interrupted", now=then)
        self.tick()
        material = self.responses.calls[0][1]["segments"]
        text = str(material)
        for marker in (IMAGE_PLACEHOLDER, UNANSWERED, INCOMPLETE):
            self.assertIn(marker, text)
        self.assertNotIn("synthetic.png", text)
        self.assertNotIn("<used>", text)

    def test_source_changed_during_extraction_cannot_auto_approve_stale_draft(self):
        original = self.seed()
        changed = []

        def hook(stage, material):
            if stage == "structure" and not changed:
                changed.append(True)
                then = business_day_bounds(self.day, self.core)[0] + timedelta(hours=2)
                body = dict(
                    model="synthetic-main",
                    messages=[dict(role="user", content="合成改写后的计划")],
                )
                revised = self.store.begin(
                    body,
                    {"operation": "edit", "target_turn_id": original["turn_id"]},
                    now=then,
                )
                self.store.finish(
                    revised, content="合成新回复", finish_reason="stop", now=then
                )

        self.responses.hook = hook
        self.tick()
        self.assertEqual(self.task()["last_error_code"], "extraction_source_changed")
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.memory_items"
                ).fetchone()["n"],
                0,
            )
        self.now += timedelta(minutes=6)
        self.tick()
        self.assertEqual(self.task()["state"], "done")
        with connect(self.app) as c:
            states = [
                r["status"]
                for r in c.execute(
                    "SELECT status FROM memory_core.pending_memories ORDER BY id"
                )
            ]
            self.assertEqual(states, ["rejected", "pending"])

    def test_retention_clears_material_copies_but_preserves_approved_card(self):
        self.seed()
        self.tick()
        self.advance_to_approval()
        self.store.expire(now=self.now + timedelta(days=31))
        with connect(self.app) as c:
            self.assertIsNone(
                c.execute(
                    "SELECT segments FROM memory_core.source_materials"
                ).fetchone()["segments"]
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.memory_items"
                ).fetchone()["n"],
                1,
            )

    def test_numeric_hash_in_source_key_is_not_mistaken_for_credentials(self):
        from memory_core.conversations.conversation_material import extraction_segments

        self.seed()

        def material(*args):
            segments, days, _ = extraction_segments(*args)
            return segments, days, "a" + "0" * 16 + "a" * 47

        with patch("memory_core.cards.daily_cards.extraction_segments", side_effect=material):
            self.tick()
        self.assertEqual(self.task()["result"]["pending_count"], 1)

    def test_semantic_setting_change_waits_for_review_and_terminal_drafts_finish_job(
        self,
    ):
        self.responses.failures["structure"] = 1
        self.seed()
        self.tick()
        self.config.set_extraction_rules(
            "one", expected_revision=1, minimum_importance=8
        )
        self.now += timedelta(minutes=6)
        self.tick()
        self.assertEqual(
            self.task()["last_error_code"], "extraction_configuration_changed"
        )
        with connect(self.app) as c:
            draft = c.execute(
                "SELECT id,version FROM memory_core.pending_memories"
            ).fetchone()
        ReviewStore(self.app, "one").reject(
            draft["id"], expected_version=draft["version"]
        )
        self.now += timedelta(minutes=6)
        self.tick()
        self.assertEqual(self.task()["state"], "done")
        self.assertEqual(self.task()["result"]["added_count"], 0)
        self.assertEqual(len(self.responses.calls), 2)

    def test_diary_failure_is_attempted_first_and_does_not_block_card_job(self):
        self.diary.fail = True
        self.responses.hook = lambda stage, material: self.assertTrue(self.diary.calls)
        self.seed()
        self.tick()
        self.assertEqual(self.task()["state"], "done")
        self.assertEqual(self.task()["result"]["pending_count"], 1)
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT state FROM memory_core.daily_tasks WHERE task_kind='diary' AND business_date=%s",
                    (self.day.isoformat(),),
                ).fetchone()["state"],
                "failed",
            )

    def test_retry_delay_starts_when_slow_model_failure_finishes(self):
        self.responses.failures["structure"] = 1
        advanced = []

        def hook(stage, material):
            if stage == "structure" and not advanced:
                advanced.append(True)
                self.now += timedelta(minutes=10)

        self.responses.hook = hook
        self.seed()
        self.tick()
        self.assertEqual(self.task()["state"], "failed")
        self.assertEqual(
            datetime.fromisoformat(self.task()["retry_at"]),
            (self.now + timedelta(minutes=5)).replace(microsecond=0),
        )
        self.tick()
        self.assertEqual(len(self.responses.calls), 2)
