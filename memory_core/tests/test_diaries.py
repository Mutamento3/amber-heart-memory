from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import psycopg

from memory_core.config import Settings
from memory_core.conversations import ConversationStore
from memory_core.daily_tasks import DailyTasks, TaskLost
from memory_core.db import connect
from memory_core.diaries import DiaryConflict, DiarySourceChanged, DiaryStore
from memory_core.install import MIGRATIONS, catalog_fingerprint, install
from memory_core.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase


class DiaryTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        self.settings = Settings(3, "America/New_York", 4)
        install(self.owner, self.settings)
        for space in ("one", "two"):
            SpaceConfiguration(self.owner).configure(
                space, Participant("小岚"), Participant("助手")
            )
        self.store = ConversationStore(self.app, "one")
        self.diaries = DiaryStore(self.app, "one")
        self.tasks = DailyTasks(self.app, "one")
        self.now = datetime.fromisoformat("2026-11-04T05:00:00-05:00")
        self.day = "2026-11-03"

    def tearDown(self):
        pass

    def seed(self, day=None, *, finish=True):
        day = day or self.day
        now = datetime.fromisoformat(day + "T05:00:00-05:00")
        body = dict(model="synthetic", messages=[dict(role="user", content="合成问题")])
        record = self.store.begin(body, {"operation": "new"}, now=now)
        if finish:
            self.store.finish(record, content="合成回复", finish_reason="stop", now=now)
        return record, body, now

    def publish(self, day=None, content="合成日记"):
        day = day or self.day
        plan = self.diaries.plan(day, now=self.now)
        task = self.tasks.claim("diary", day, now=self.now)
        return self.diaries.publish(
            plan, dict(content=content, model="synthetic-diary"), task, now=self.now
        )

    def test_body_only_material_and_pending_tool_round_waits(self):
        SpaceConfiguration(self.owner).set_core_memory(
            "one", expected_revision=1, core_memory="小岚喜欢在周末做陶艺。"
        )
        record, body, then = self.seed(finish=False)
        call = dict(
            id="call-1",
            type="function",
            function=dict(name="secret_tool", arguments="secret_arguments"),
        )
        self.store.finish(
            record,
            content="工具前正文",
            tools=[call],
            finish_reason="tool_calls",
            now=then,
        )
        self.assertEqual(
            self.diaries.plan(self.day, now=self.now)["reason"], "diary_turn_pending"
        )
        continued = deepcopy(body)
        continued["messages"] += [
            dict(role="assistant", content="工具前正文", tool_calls=[call]),
            dict(role="tool", tool_call_id="call-1", content="secret_tool_result"),
        ]
        step = self.store.begin(continued, {}, now=then)
        self.store.finish(step, content="最后的正文", finish_reason="stop", now=then)
        plan = self.diaries.plan(self.day, now=self.now)
        self.assertIn("小岚", plan["prompt"])
        self.assertIn("【我熟悉的小岚】\n小岚喜欢在周末做陶艺。", plan["material"])
        self.assertIn("工具前正文", plan["material"])
        self.assertIn("最后的正文", plan["material"])
        for secret in (
            "secret_tool",
            "secret_arguments",
            "secret_tool_result",
            "<used>",
        ):
            self.assertNotIn(secret, plan["material"])

    def test_empty_unfinished_day_and_expired_material_are_not_generated(self):
        self.assertEqual(
            self.diaries.plan(self.day, now=self.now)["reason"], "no_dialogue"
        )
        self.assertEqual(
            self.diaries.plan("2026-11-04", now=self.now)["reason"],
            "diary_day_not_finished",
        )
        self.seed("2026-10-01")
        self.assertEqual(
            self.diaries.plan("2026-10-01", now=self.now)["reason"], "source_expired"
        )

    def test_failed_rounds_images_and_partial_replies_keep_their_meaning(self):
        record, body, then = self.seed(finish=False)
        self.store.finish(record, state="upstream_failed", now=then)
        body["messages"][0]["content"] = [
            dict(type="text", text="图片问题"),
            dict(
                type="image_url", image_url=dict(url="data:image/png;base64,NOT_SAVED")
            ),
        ]
        other = self.store.begin(body, {"operation": "new"}, now=then)
        self.store.finish(other, content="半段正文", state="interrupted", now=then)
        material = self.diaries.plan(self.day, now=self.now)["material"]
        for marker in ("未获回应", "未完成", "图片内容未保留", "半段正文"):
            self.assertIn(marker, material)
        self.assertNotIn("NOT_SAVED", material)

    def test_source_and_configuration_changes_reject_the_result(self):
        record, _, _ = self.seed()
        plan = self.diaries.plan(self.day, now=self.now)
        task = self.tasks.claim("diary", self.day, now=self.now)
        with connect(self.owner) as c:
            c.execute(
                'UPDATE memory_core.conversation_messages SET content_parts=\'[{"type":"text","text":"变化后的正文"}]\' WHERE turn_id=%s AND role=\'user\'',
                (record["turn_id"],),
            )
        with self.assertRaises(DiarySourceChanged):
            self.diaries.publish(
                plan, dict(content="旧结果", model="synthetic"), task, now=self.now
            )
        plan = self.diaries.plan(self.day, now=self.now)
        with connect(self.owner) as c:
            c.execute(
                "UPDATE memory_core.space_policies SET revision=revision+1 WHERE bot_id='one'"
            )
        with self.assertRaises(DiarySourceChanged):
            self.diaries.publish(
                plan, dict(content="旧结果", model="synthetic"), task, now=self.now
            )
        self.assertIsNone(self.diaries.get(self.day))

    def test_publication_and_task_completion_are_one_transaction(self):
        self.seed()
        plan = self.diaries.plan(self.day, now=self.now)
        task = self.tasks.claim("diary", self.day, now=self.now)
        with patch(
            "memory_core.diaries.finish_task",
            side_effect=RuntimeError("synthetic commit failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.diaries.publish(
                    plan,
                    dict(content="合成日记", model="synthetic"),
                    task,
                    now=self.now,
                )
        self.assertIsNone(self.diaries.get(self.day))
        with connect(self.app) as c:
            self.assertEqual(
                c.execute("SELECT state FROM memory_core.daily_tasks").fetchone()[
                    "state"
                ],
                "running",
            )

    def test_attempt_fence_lock_restart_and_completed_key(self):
        self.seed()
        plan = self.diaries.plan(self.day, now=self.now)
        with self.tasks.run_lock() as first:
            with DailyTasks(self.app, "one").run_lock() as duplicate:
                self.assertTrue(first)
                self.assertFalse(duplicate)
            old = self.tasks.claim("diary", self.day, now=self.now)
        with self.tasks.run_lock() as restarted:
            self.assertTrue(restarted)
            current = self.tasks.claim("diary", self.day, now=self.now)
            with self.assertRaises(TaskLost):
                self.diaries.publish(
                    plan, dict(content="迟到结果", model="synthetic"), old, now=self.now
                )
            self.diaries.publish(
                plan, dict(content="新结果", model="synthetic"), current, now=self.now
            )
            self.assertIsNone(self.tasks.claim("diary", self.day, now=self.now))
        self.assertEqual(self.diaries.get(self.day)["content"], "新结果")

    def test_retry_deadline_and_cross_space_task_access(self):
        task = self.tasks.claim("diary", self.day, now=self.now)
        self.tasks.fail(task, "synthetic_failure", now=self.now)
        self.assertIsNone(
            self.tasks.claim("diary", self.day, now=self.now + timedelta(minutes=4))
        )
        retry = self.tasks.claim("diary", self.day, now=self.now + timedelta(minutes=5))
        self.assertEqual(retry["attempts"], 2)
        with self.assertRaises(ValueError):
            DailyTasks(self.app, "two").finish(retry, now=self.now)

    def test_edit_is_versioned_space_bound_and_survives_retention(self):
        self.seed()
        identifier = self.publish()
        saved = self.diaries.edit(identifier, "手动修改的日记", 1, now=self.now)
        self.assertTrue(saved["user_edited"])
        self.assertEqual(saved["revision"], 2)
        with self.assertRaises(DiaryConflict):
            self.diaries.edit(identifier, "陈旧编辑", 1, now=self.now)
        with self.assertRaises(DiaryConflict):
            DiaryStore(self.app, "two").edit(identifier, "跨空间编辑", 2, now=self.now)
        self.store.expire(now=self.now + timedelta(days=31))
        self.assertEqual(self.diaries.get(self.day)["content"], "手动修改的日记")

    def test_recent_three_prefix_changes_only_when_new_diary_is_published(self):
        for day in ("2026-10-31", "2026-11-01", "2026-11-02", self.day):
            self.seed(day)
            if day != self.day:
                self.publish(day, content="日记" + day)

        def context():
            body = dict(
                model="synthetic", messages=[dict(role="user", content="今天的一句")]
            )
            record = self.store.begin(body, {"operation": "new"}, now=self.now)
            source = self.store.history_input(record)
            sent = self.store.save_context(
                record, body, source, dict(cards=[], status="empty")
            )
            self.store.finish(
                record, content="回应", finish_reason="stop", now=self.now
            )
            return sent["messages"][0]["content"], source

        first, source = context()
        second, _ = context()
        self.assertEqual(first.encode(), second.encode())
        self.assertEqual([row["business_date"] for row in source["diaries"]], ["2026-11-01", "2026-11-02"])
        self.assertNotIn("日记2026-10-31", first)
        self.assertIn("日记中的「她」指小岚，但你在正文回复中必须用「你」称呼小岚，禁止用「她」。", first)
        self.publish()
        changed, source = context()
        repeated, _ = context()
        self.assertNotEqual(first, changed)
        self.assertEqual(changed.encode(), repeated.encode())
        self.assertEqual(len(source["diaries"]), 3)

    def test_v12_upgrade_failure_rollback_and_repeat(self):
        with connect(self.owner) as c:
            c.execute("DROP SCHEMA memory_core CASCADE")
        install(self.owner, self.settings, target_version=12)
        with connect(self.owner) as c:
            before = catalog_fingerprint(c)
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(MIGRATIONS, Path(directory) / "migrations")
            path = Path(directory) / "migrations/013_diaries_and_daily_tasks.sql"
            path.write_text(path.read_text() + "\nSELECT 1/0;")
            with patch(
                "memory_core.install.MIGRATIONS", path.parent
            ), self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner, self.settings)
        with connect(self.owner) as c:
            self.assertEqual(catalog_fingerprint(c), before)
        self.assertTrue(install(self.owner, self.settings))
        self.assertFalse(install(self.owner, self.settings))

    def test_tool_continuation_keeps_diary_snapshot_when_new_diary_arrives(self):
        self.seed()
        body = dict(
            model="synthetic", messages=[dict(role="user", content="本轮使用工具")]
        )
        record = self.store.begin(body, {"operation": "new"}, now=self.now)
        frozen = self.store.save_context(
            record,
            body,
            self.store.history_input(record),
            dict(cards=[], status="empty"),
        )
        call = dict(
            id="call-2", type="function", function=dict(name="合成工具", arguments="{}")
        )
        self.store.finish(
            record,
            content="先调用工具",
            tools=[call],
            finish_reason="tool_calls",
            now=self.now,
        )
        self.publish()
        body["messages"] += [
            dict(role="assistant", content="先调用工具", tool_calls=[call]),
            dict(role="tool", tool_call_id="call-2", content="合成结果"),
        ]
        continued = self.store.begin(body, {}, now=self.now)
        self.assertEqual(
            continued["upstream_body"]["messages"][:-2], frozen["messages"]
        )
