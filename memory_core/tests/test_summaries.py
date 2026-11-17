from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import psycopg

from memory_core.config import Settings
from memory_core.conversations import ConversationStore, ConversationConflict
from memory_core.db import connect
from memory_core.install import install, MIGRATIONS, catalog_fingerprint
from memory_core.space import Participant, SpaceConfiguration
from memory_core.summaries import SummaryStore, active_summaries
from memory_core.summary_policy import SummarySettings
from memory_core.tests.support import DatabaseTestCase


class SummaryTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        self.settings = Settings(3, "America/New_York", 4)
        install(self.owner, self.settings)
        self.config = SpaceConfiguration(self.owner)
        for space in ("one", "two"):
            self.config.configure(space, Participant("小岚"), Participant("助手"))
        self.store = ConversationStore(self.app, "one")
        self.summary = SummaryStore(self.app, "one")
        self.now = datetime.fromisoformat("2026-11-02T05:00:00-05:00")
        self.day = "2026-11-02"

    def tearDown(self):
        pass

    def seed(self, count, *, day=None, long=False):
        records = []
        moment = (
            self.now if day is None else datetime.fromisoformat(day + "T05:00:00-05:00")
        )
        for i in range(count):
            body = {
                "model": "synthetic-chat",
                "messages": [
                    {
                        "role": "user",
                        "content": "合成问句" + str(i) + ("长" * 100 if long else ""),
                    }
                ],
            }
            record = self.store.begin(
                body, {"operation": "new"}, now=moment + timedelta(seconds=i)
            )
            self.store.finish(
                record,
                content="合成回复" + str(i),
                finish_reason="stop",
                now=moment + timedelta(seconds=i),
            )
            records.append(record)
        return records

    def save_next(self, content="合成摘要"):
        plan = self.summary.next_plan(self.day)
        self.assertIsNotNone(plan)
        return self.summary.save(
            plan, {"content": content, "model": "synthetic-summary"}, now=self.now
        )

    def test_fourteen_rounds_count_twice_and_keep_last_two_with_exact_coverage(self):
        records = self.seed(13)
        self.assertIsNone(self.summary.next_plan(self.day))
        records += self.seed(1)
        plan = self.summary.next_plan(self.day)
        self.assertEqual(plan["source_ids"], [r["turn_id"] for r in records[:12]])
        self.assertIn("小岚", plan["prompt"])
        identifier = self.summary.save(
            plan, {"content": "合成摘要", "model": "synthetic-summary"}, now=self.now
        )
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summary_turns WHERE summary_id=%s",
                    (identifier,),
                ).fetchone()["n"],
                12,
            )
            self.assertEqual(len(active_summaries(c, "one", self.day)), 1)
        self.assertIsNone(self.summary.next_plan(self.day))
        # The latest adopted round is still a relay and can be regenerated.
        body = {
            "model": "synthetic-chat",
            "messages": [{"role": "user", "content": "合成问句0"}],
        }
        replacement = self.store.begin(body, {"operation": "edit"}, now=self.now)
        self.store.finish(
            replacement, content="重做回复", finish_reason="stop", now=self.now
        )
        self.assertNotEqual(replacement["turn_id"], records[-1]["turn_id"])

    def test_first_level_uses_three_latest_previous_summaries_and_shows_business_day_bounds(self):
        self.seed(14, day="2026-11-01")
        previous_day = self.summary.next_plan("2026-11-01")
        self.summary.save(previous_day, dict(content="前一日摘要", model="synthetic"))
        recent = [f"前情记录{i}" for i in range(4)]
        for content in recent:
            self.seed(14)
            self.save_next(content)
        self.seed(14)
        plan = self.summary.next_plan(self.day)
        self.assertEqual(plan["level"], 1)
        self.assertIn("450 字以内", plan["prompt"])
        self.assertIn("避免重复记录；不要照抄其内容，也不要模仿其写法或开头格式", plan["material"])
        self.assertIn("\n".join(f"- {content}" for content in recent[-3:]), plan["material"])
        self.assertNotIn(recent[0], plan["material"])
        self.assertNotIn("前一日摘要", plan["material"])

        body = dict(model="synthetic", messages=[dict(role="user", content="天亮前的消息")])
        after_midnight = datetime.fromisoformat("2026-11-03T01:00:00-05:00")
        record = self.store.begin(body, {"operation": "new"}, now=after_midnight)
        source = self.store.history_input(record)
        sent = self.store.save_context(record, body, source, dict(status="empty", cards=[]))
        self.assertIn("今日（11月2日 04:00 ~ 11月3日 04:00）", sent["messages"][0]["content"])

    def test_append_during_model_call_is_allowed_but_source_version_change_is_rejected(
        self,
    ):
        records = self.seed(14)
        plan = self.summary.next_plan(self.day)
        self.seed(1)
        self.assertIsNotNone(
            self.summary.save(
                plan, {"content": "允许新轮加入", "model": "synthetic"}, now=self.now
            )
        )
        self.seed(12)
        plan = self.summary.next_plan(self.day)
        with connect(self.app) as c:
            c.execute(
                "UPDATE memory_core.conversation_turns SET history_state='replaced' WHERE id=%s",
                (plan["source_ids"][0],),
            )
        self.assertIsNone(
            self.summary.save(
                plan, {"content": "过期结果", "model": "synthetic"}, now=self.now
            )
        )

    def test_source_body_or_configuration_change_discards_result_without_consuming_raw(
        self,
    ):
        self.seed(14)
        plan = self.summary.next_plan(self.day)
        with connect(self.app) as c:
            c.execute(
                'UPDATE memory_core.conversation_messages SET content_parts=\'[{"type":"text","text":"已变化"}]\' WHERE turn_id=%s AND role=\'user\'',
                (plan["source_ids"][0],),
            )
        self.assertIsNone(
            self.summary.save(
                plan, {"content": "不能保存", "model": "synthetic"}, now=self.now
            )
        )
        plan = self.summary.next_plan(self.day)
        self.config.set_persona("one", expected_revision=1, persona="中性设置变化")
        self.assertIsNone(
            self.summary.save(
                plan, {"content": "旧配置结果", "model": "synthetic"}, now=self.now
            )
        )
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summary_turns"
                ).fetchone()["n"],
                0,
            )

    def test_tool_steps_do_not_increase_count_and_payload_never_enters_material(self):
        self.seed(2)
        self.summary = SummaryStore(
            self.app, "one", SummarySettings(trigger_messages=6, retain_turns=1)
        )
        body = {
            "model": "synthetic",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,NOT_SAVED"},
                        }
                    ],
                }
            ],
        }
        record = self.store.begin(body, {"operation": "new"}, now=self.now)
        call = {
            "id": "tool-1",
            "type": "function",
            "function": {
                "name": "secret_tool_name",
                "arguments": '{"secret":"tool payload"}',
            },
        }
        self.store.finish(
            record,
            content="调用前正文",
            tools=[call],
            finish_reason="tool_calls",
            now=self.now,
        )
        self.assertIsNone(self.summary.next_plan(self.day))
        continuation = deepcopy(body)
        continuation["messages"] += [
            {"role": "assistant", "content": "调用前正文", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "tool-1", "content": "secret result"},
        ]
        step = self.store.begin(continuation, {}, now=self.now)
        self.store.finish(step, content="结束正文", finish_reason="stop", now=self.now)
        self.seed(1)
        plan = self.summary.next_plan(self.day)
        first = self.summary.save(
            plan, {"content": "第一摘要", "model": "synthetic"}, now=self.now
        )
        self.seed(2)
        plan = self.summary.next_plan(self.day)
        material = plan["material"]
        for expected in ("调用前正文", "结束正文", "图片内容未保留"):
            self.assertIn(expected, material)
        for forbidden in (
            "secret_tool_name",
            "tool payload",
            "secret result",
            "NOT_SAVED",
            "<used>",
        ):
            self.assertNotIn(forbidden, material)

    def test_eight_first_summaries_merge_oldest_three_once_and_keep_sources(self):
        self.summary = SummaryStore(
            self.app, "one", SummarySettings(trigger_messages=4, retain_turns=1)
        )
        self.seed(9)
        leaves = [self.save_next("一级" + str(i)) for i in range(8)]
        plan = self.summary.next_plan(self.day)
        self.assertEqual(plan["level"], 2)
        self.assertEqual(plan["source_ids"], leaves[:3])
        self.assertNotIn("合成问句", plan["material"])
        merged = self.summary.save(
            plan, {"content": "二次合并", "model": "synthetic"}, now=self.now
        )
        with connect(self.app) as c:
            view = active_summaries(c, "one", self.day)
            self.assertEqual([s["id"] for s in view], [merged] + leaves[3:])
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summaries"
                ).fetchone()["n"],
                9,
            )
        self.assertIsNone(self.summary.next_plan(self.day))

    def test_summary_is_stable_prefix_and_only_current_business_day_is_injected(self):
        records = self.seed(14)
        identifier = self.save_next("固定摘要前缀")
        body = {
            "model": "synthetic",
            "messages": [{"role": "user", "content": "下一句"}],
        }
        record = self.store.begin(body, {"operation": "new"}, now=self.now)
        source = self.store.history_input(record)
        self.assertEqual(
            source["history_turn_ids"], [r["turn_id"] for r in records[-2:]]
        )
        upstream = self.store.save_context(
            record, body, source, {"status": "empty", "cards": []}
        )
        self.assertEqual(upstream["messages"][0]["role"], "system")
        self.assertIn("固定摘要前缀", upstream["messages"][0]["content"])
        self.store.finish(record, content="回复", finish_reason="stop", now=self.now)
        second = self.store.begin(body, {"operation": "new"}, now=self.now)
        replay = self.store.save_context(
            second,
            body,
            self.store.history_input(second),
            {"status": "empty", "cards": []},
        )
        self.assertEqual(
            upstream["messages"][:-3],
            replay["messages"][: len(upstream["messages"][:-3])],
        )
        import json

        wire = lambda messages: json.dumps(
            messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        self.assertEqual(
            wire(upstream["messages"][:-3]),
            wire(replay["messages"][: len(upstream["messages"][:-3])]),
        )
        self.store.finish(second, content="回复", finish_reason="stop", now=self.now)
        tomorrow = self.store.begin(
            body, {"operation": "new"}, now=self.now + timedelta(days=1)
        )
        new_source = self.store.history_input(tomorrow)
        self.assertEqual(new_source["summaries"], [])

    def test_long_history_has_explicit_notice_and_actual_manifest_of_omitted_turns(
        self,
    ):
        records = self.seed(3, long=True)
        body = {
            "model": "synthetic",
            "messages": [{"role": "user", "content": "新一句"}],
        }
        record = self.store.begin(body, {"operation": "new"}, now=self.now)
        source = self.store.history_input(record, max_history_chars=150)
        self.assertEqual(source["history_omitted"]["turns"], 2)
        upstream = self.store.save_context(
            record, body, source, {"status": "empty", "cards": []}
        )
        self.assertIn(
            "[更早的2轮原话这轮没有带上；不要假装看过，涉及细节时请向小岚确认。]",
            str(upstream),
        )
        self.assertEqual(source["history_turn_ids"], [records[-1]["turn_id"]])
        with connect(self.app) as c:
            manifest = c.execute(
                "SELECT context_manifest FROM memory_core.conversation_turns WHERE id=%s",
                (record["turn_id"],),
            ).fetchone()["context_manifest"]
            self.assertEqual(
                manifest["history_omitted"]["turn_ids"],
                [r["turn_id"] for r in records[:2]],
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_messages"
                ).fetchone()["n"],
                7,
            )

    def test_cross_space_and_business_day_coverage_is_rejected_by_database(self):
        records = self.seed(14)
        identifier = self.save_next()
        other = ConversationStore(self.app, "two")
        record = other.begin(
            {
                "model": "synthetic",
                "messages": [{"role": "user", "content": "别的空间"}],
            },
            {},
            now=self.now,
        )
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), connect(
            self.app
        ) as c:
            c.execute(
                "INSERT INTO memory_core.conversation_summary_turns(summary_id,bot_id,business_date,turn_id) VALUES (%s,'one',%s,%s)",
                (identifier, self.day, record["turn_id"]),
            )
        tomorrow = self.store.begin(
            {
                "model": "synthetic",
                "messages": [{"role": "user", "content": "下一业务日"}],
            },
            {"operation": "new"},
            now=self.now + timedelta(days=1),
        )
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), connect(
            self.app
        ) as c:
            c.execute(
                "INSERT INTO memory_core.conversation_summary_turns(summary_id,bot_id,business_date,turn_id) VALUES (%s,'one',%s,%s)",
                (identifier, self.day, tomorrow["turn_id"]),
            )

    def test_expiry_clears_summary_and_rollup_text_with_raw_but_keeps_coverage(self):
        self.summary = SummaryStore(
            self.app,
            "one",
            SummarySettings(trigger_messages=4, retain_turns=1, rollup_trigger=3),
        )
        self.seed(4)
        leaves = [self.save_next("一级") for i in range(3)]
        merged = self.save_next("二次")
        self.store.expire(now=self.now + timedelta(days=31))
        with connect(self.app) as c:
            self.assertTrue(
                all(
                    s["content"] is None
                    for s in c.execute(
                        "SELECT content FROM memory_core.conversation_summaries"
                    )
                )
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summary_turns"
                ).fetchone()["n"],
                3,
            )

    def test_summaries_expire_after_fifteen_days_while_raw_conversation_stays(self):
        self.summary = SummaryStore(
            self.app,
            "one",
            SummarySettings(trigger_messages=4, retain_turns=1, rollup_trigger=3),
        )
        self.seed(4)
        self.save_next("一级")
        self.store.expire(now=self.now + timedelta(days=14))
        with connect(self.app) as c:
            self.assertIsNotNone(c.execute("SELECT content FROM memory_core.conversation_summaries").fetchone()["content"])
        self.store.expire(now=self.now + timedelta(days=16))
        with connect(self.app) as c:
            self.assertIsNone(c.execute("SELECT content FROM memory_core.conversation_summaries").fetchone()["content"])
            self.assertEqual(c.execute(
                "SELECT count(*) AS n FROM memory_core.conversation_turns WHERE content_deleted_at IS NULL").fetchone()["n"] > 0, True)

    def test_cross_worker_job_lock_prevents_duplicate_model_work(self):
        other = SummaryStore(self.app, "one")
        with self.summary.job_lock() as first:
            with other.job_lock() as second:
                self.assertTrue(first)
                self.assertFalse(second)
        with other.job_lock() as after:
            self.assertTrue(after)

    def test_failed_and_partial_rounds_have_explicit_annotations_in_summary_input(self):
        self.summary = SummaryStore(
            self.app, "one", SummarySettings(trigger_messages=4, retain_turns=1)
        )
        body = {
            "model": "synthetic",
            "messages": [{"role": "user", "content": "未被回应的问题"}],
        }
        first = self.store.begin(body, {"operation": "new"}, now=self.now)
        self.store.finish(first, state="upstream_failed", now=self.now)
        body["messages"][0]["content"] = "中断的问题"
        second = self.store.begin(body, {"operation": "new"}, now=self.now)
        self.store.finish(second, content="半段正文", state="interrupted", now=self.now)
        self.seed(1)
        plan = self.summary.next_plan(self.day)
        self.assertIn("未获回应", plan["material"])
        self.summary.save(
            plan, {"content": "失败用户句摘要", "model": "synthetic"}, now=self.now
        )
        plan = self.summary.next_plan(self.day)
        self.assertIn("半段正文", plan["material"])
        self.assertIn("未完成", plan["material"])

    def test_covered_version_guard_and_read_time_coverage_fallback(self):
        records = self.seed(14)
        identifier = self.save_next("旧版本摘要")
        with connect(self.app) as c:
            c.execute(
                "INSERT INTO memory_core.conversation_summary_turns(summary_id,bot_id,business_date,turn_id) VALUES (%s,'one',%s,%s)",
                (identifier, self.day, records[-1]["turn_id"]),
            )
        body = {"model": "synthetic", "messages": [{"role": "user", "content": "改写"}]}
        with self.assertRaises(ConversationConflict) as conflict:
            self.store.begin(body, {"operation": "edit"}, now=self.now)
        self.assertEqual(conflict.exception.code, "summary_covered")
        with connect(self.app) as c:
            c.execute(
                "UPDATE memory_core.conversation_turns SET history_state='replaced' WHERE id=%s",
                (records[0]["turn_id"],),
            )
        record = self.store.begin(body, {"operation": "new"}, now=self.now)
        source = self.store.history_input(record)
        self.assertEqual(source["summaries"], [])
        self.assertIn(records[1]["turn_id"], source["history_turn_ids"])

    def test_summary_and_coverage_commit_together_on_insert_failure(self):
        self.seed(14)
        plan = self.summary.next_plan(self.day)

        @contextmanager
        def fail_second_coverage(dsn):
            with connect(dsn) as connection:

                class Connection:
                    inserted = 0

                    def execute(self, query, parameters=None):
                        if (
                            "INSERT INTO memory_core.conversation_summary_turns"
                            in query
                        ):
                            self.inserted += 1
                            if self.inserted == 2:
                                raise RuntimeError("synthetic second coverage failure")
                        return connection.execute(query, parameters)

                yield Connection()

        with patch(
            "memory_core.summaries.connect", fail_second_coverage
        ), self.assertRaises(RuntimeError):
            self.summary.save(
                plan, {"content": "有效正文", "model": "synthetic"}, now=self.now
            )
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summaries"
                ).fetchone()["n"],
                0,
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_summary_turns"
                ).fetchone()["n"],
                0,
            )

    def test_v11_upgrade_repeat_and_failure_leave_data_and_schema_unchanged(self):
        with connect(self.owner) as c:
            c.execute("DROP SCHEMA memory_core CASCADE")
        install(self.owner, self.settings, target_version=11)
        SpaceConfiguration(self.owner).configure(
            "old", Participant("用户"), Participant("助手")
        )
        with connect(self.owner) as c:
            before = catalog_fingerprint(c)
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(directory) / "migrations"
            shutil.copytree(MIGRATIONS, migrations)
            path = migrations / "012_conversation_summaries.sql"
            path.write_text(path.read_text() + "\nSELECT 1/0;")
            with patch("memory_core.install.MIGRATIONS", migrations), self.assertRaises(
                psycopg.errors.DivisionByZero
            ):
                install(self.owner, self.settings)
        with connect(self.owner) as c:
            self.assertEqual(catalog_fingerprint(c), before)
        self.assertTrue(install(self.owner, self.settings))
        self.assertFalse(install(self.owner, self.settings))
