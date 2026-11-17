from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from copy import deepcopy

from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore, ConversationConflict, DuplicateRequest
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.storage.space import SpaceConfiguration, Participant
from memory_core.tests.support import DatabaseTestCase


def body(text, after=None):
    messages = [] if after is None else [{"role": "assistant", "content": after}]
    return {"model": "synthetic", "messages": messages + [{"role": "user", "content": text}]}


def call(identifier):
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": "synthetic_tool", "arguments": "{}"},
    }


class TurnTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        install(self.owner, Settings(3))
        for space in ("one", "two"):
            SpaceConfiguration(self.owner).configure(
                space, Participant("用户"), Participant("助手")
            )
        self.store = ConversationStore(self.app, "one")

    def turn(self, record):
        with connect(self.app) as c:
            return c.execute(
                "SELECT * FROM memory_core.conversation_turns WHERE id=%s", (record["turn_id"],)
            ).fetchone()

    def exchange(self, text, reply, after=None, intent=None):
        record = self.store.begin(body(text, after), intent or {})
        self.store.finish(record, content=reply, finish_reason="stop")
        return record

    def test_anchor_four_cases_regenerate_edit_stale_device_and_earlier_edit(self):
        first = self.exchange("第一句", "回复一")
        second = self.exchange("第二句", "回复二", "回复一")
        replacement = self.store.begin(body("第二句", "回复一"), {})
        self.assertEqual(self.turn(replacement)["replaces_turn_id"], second["turn_id"])
        self.assertEqual(self.turn(second)["history_state"], "current")
        self.store.finish(replacement, content="回复二新版", finish_reason="stop")
        self.assertEqual(self.turn(second)["history_state"], "replaced")
        edited = self.exchange("第二句改字", "改字后的回复", "回复一")
        self.assertEqual(self.turn(edited)["position_id"], self.turn(second)["position_id"])
        third = self.exchange("第三句", "回复三", "改字后的回复")
        stale = self.exchange("旧设备的新一句", "回复四", "回复一")
        self.assertIsNone(self.turn(stale)["replaces_turn_id"])
        earlier_edit = self.exchange("修改更早的第一句", "回复五")
        self.assertIsNone(self.turn(earlier_edit)["replaces_turn_id"])
        unmatched = self.exchange("另一端的话", "回复六", "后端从未见过的回复")
        self.assertIsNone(self.turn(unmatched)["replaces_turn_id"])
        self.assertEqual(self.turn(first)["history_state"], "current")
        self.assertEqual(self.turn(third)["history_state"], "current")

    def test_first_message_same_different_text_and_explicit_intent_priority(self):
        first = self.exchange("嗯", "一")
        same = self.exchange("嗯", "二")
        self.assertEqual(self.turn(same)["replaces_turn_id"], first["turn_id"])
        different = self.exchange("好", "三")
        self.assertIsNone(self.turn(different)["replaces_turn_id"])
        explicit_new = self.exchange("好", "四", intent={"operation": "new"})
        self.assertIsNone(self.turn(explicit_new)["replaces_turn_id"])
        with self.assertRaises(ConversationConflict):
            self.store.begin(
                body("改旧句"), {"operation": "edit", "target_turn_id": first["turn_id"]}
            )
        explicit_edit = self.exchange(
            "改最后句",
            "五",
            intent={"operation": "edit", "target_turn_id": explicit_new["turn_id"]},
        )
        self.assertEqual(self.turn(explicit_edit)["replaces_turn_id"], explicit_new["turn_id"])

    def test_failed_and_interrupted_replacement_never_displace_original(self):
        original = self.exchange("原话", "原回复")
        for state in ("upstream_failed", "interrupted", "archive_failed"):
            record = self.store.begin(body("原话"), {})
            self.store.finish(record, content="半段", state=state)
            self.assertEqual(self.turn(original)["history_state"], "current")
            self.assertEqual(self.turn(record)["history_state"], "candidate")
            self.assertIsNone(self.turn(record)["frozen_context"])
        failed = self.store.begin(body("新的一句"), {"operation": "new"})
        self.store.finish(failed, state="upstream_failed")
        retry = self.store.begin(
            body("新的一句"), {"operation": "retry", "target_turn_id": failed["turn_id"]}
        )
        self.store.finish(retry, content="重试回复", finish_reason="stop")
        self.assertEqual(self.turn(failed)["history_state"], "replaced")
        with connect(self.app) as c:
            row = c.execute(
                "SELECT * FROM memory_core.conversation_requests WHERE id=%s",
                (retry["request_id"],),
            ).fetchone()
            self.assertEqual(row["retry_of_request_id"], failed["request_id"])

    def test_visible_downstream_body_controls_anchor_matching(self):
        first = self.store.begin(body("第一句"), {})
        self.store.finish(
            first,
            content="可见回复<used>1</used>",
            visible_content="可见回复",
            finish_reason="stop",
        )
        second = self.store.begin(body("第二句", "可见回复"), {})
        self.assertEqual(self.turn(second)["anchor_kind"], "matched")
        self.assertIsNone(self.turn(second)["replaces_turn_id"])

    def unanswered(self, state, *, with_anchor=True):
        if with_anchor:
            self.exchange("前一轮", "上一轮的回复", intent={"operation": "new"})
        request = body("失败的这一句", "上一轮的回复" if with_anchor else None)
        record = self.store.begin(request, {"operation": "new"})
        self.store.finish(record, state=state)
        return record, request

    def test_unanswered_same_text_at_original_anchor_is_regeneration(self):
        for state in ("upstream_failed", "interrupted"):
            with self.subTest(state=state):
                failed, request = self.unanswered(state)
                # Third-party client: only assistant anchor + user, no memory.
                retry = self.store.begin(request, {})
                try:
                    self.assertEqual(self.turn(retry)["replaces_turn_id"], failed["turn_id"])
                    self.assertEqual(self.turn(retry)["position_id"], self.turn(failed)["position_id"])
                    self.assertEqual(self.turn(failed)["history_state"], "current")
                    with connect(self.app) as c:
                        operation = c.execute('SELECT resolved_operation FROM memory_core.conversation_requests WHERE id=%s', (retry['request_id'],)).fetchone()['resolved_operation']
                        self.assertEqual(operation, 'regenerate')
                finally:
                    self.store.finish(retry, content="重发成功", finish_reason="stop")

    def test_unanswered_rewritten_text_at_original_anchor_is_edit(self):
        for state in ("upstream_failed", "interrupted"):
            with self.subTest(state=state):
                failed, request = self.unanswered(state)
                request["messages"][-1]["content"] = "改写后的这一句"
                edited = self.store.begin(request, {})
                try:
                    self.assertEqual(self.turn(edited)["replaces_turn_id"], failed["turn_id"])
                    with connect(self.app) as c:
                        operation = c.execute('SELECT resolved_operation FROM memory_core.conversation_requests WHERE id=%s', (edited['request_id'],)).fetchone()['resolved_operation']
                        self.assertEqual(operation, 'edit')
                finally:
                    self.store.finish(edited, content="改写成功", finish_reason="stop")

    def test_retaining_unanswered_user_in_frontend_history_means_new_message(self):
        for state in ("upstream_failed", "interrupted"):
            for with_anchor in (True, False):
                for text in ("接着说的新话", "失败的这一句"):
                    with self.subTest(state=state, with_anchor=with_anchor, text=text):
                        failed, request = self.unanswered(state, with_anchor=with_anchor)
                        request["messages"].append({"role": "user", "content": text})
                        new = self.store.begin(request, {})
                        try:
                            self.assertIsNone(self.turn(new)["replaces_turn_id"])
                            self.assertEqual(self.turn(failed)["history_state"], "current")
                        finally:
                            self.store.finish(new, content="继续聊天", finish_reason="stop")

    def test_partial_answer_regeneration_still_replaces_its_original_round(self):
        self.exchange("前一轮", "上一轮的回复")
        request = body("失败的这一句", "上一轮的回复")
        partial = self.store.begin(request, {})
        self.store.finish(partial, content="已经收到的半段正文", state="interrupted")
        retry = self.store.begin(request, {})
        self.assertEqual(self.turn(retry)["replaces_turn_id"], partial["turn_id"])
        self.assertEqual(self.turn(partial)["history_state"], "current")
        self.store.finish(retry, content="完整回复", finish_reason="stop")
        self.assertEqual(self.turn(partial)["history_state"], "replaced")

    def test_unanswered_first_message_without_anchor_keeps_same_text_fallback(self):
        for text, replaces in (("失败的这一句", True), ("另一个首句", False)):
            with self.subTest(text=text):
                failed, _ = self.unanswered("upstream_failed", with_anchor=False)
                new = self.store.begin(body(text), {})
                try:
                    self.assertEqual(self.turn(new)["replaces_turn_id"], failed["turn_id"] if replaces else None)
                finally:
                    self.store.finish(new, content="回复", finish_reason="stop")

    def test_multistep_tool_round_freezes_prefix_and_owns_business_day(self):
        start = datetime.fromisoformat("2026-11-01T02:59:30+08:00")
        initial = body("查一下")
        initial["messages"].insert(0, {"role": "system", "content": "冻结的系统词"})
        initial["tools"] = [
            {
                "type": "function",
                "function": {"name": "synthetic_tool", "parameters": {"type": "object"}},
            }
        ]
        first = self.store.begin(initial, {"request_id": "step0"}, now=start)
        self.store.finish(
            first,
            content="先查一次",
            tools=[call("a")],
            finish_reason="tool_calls",
            now=start + timedelta(seconds=1),
        )
        with self.assertRaises(ConversationConflict):
            self.store.begin(body("另一端插话"), {}, now=start + timedelta(seconds=2))
        continuation = deepcopy(initial)
        continuation["messages"][0]["content"] = "前端后来改了系统词"
        continuation["messages"] += [
            {"role": "assistant", "content": "先查一次", "tool_calls": [call("a")]},
            {"role": "tool", "tool_call_id": "a", "content": "结果一"},
        ]
        second = self.store.begin(
            continuation, {"request_id": "step1"}, now=start + timedelta(seconds=60)
        )
        self.assertEqual(second["turn_id"], first["turn_id"])
        self.assertEqual(second["step_index"], 1)
        self.assertEqual(second["upstream_body"]["messages"][0]["content"], "冻结的系统词")
        self.store.finish(
            second,
            content="再查一次",
            tools=[call("b")],
            finish_reason="tool_calls",
            now=start + timedelta(seconds=61),
        )
        continuation["messages"] += [
            {"role": "assistant", "content": "再查一次", "tool_calls": [call("b")]},
            {"role": "tool", "tool_call_id": "b", "content": "结果二"},
        ]
        third = self.store.begin(
            continuation, {"request_id": "step2"}, now=start + timedelta(seconds=120)
        )
        self.store.finish(
            third, content="最终回复", finish_reason="stop", now=start + timedelta(seconds=121)
        )
        self.assertEqual(self.turn(first)["business_date"], "2026-10-31")
        self.assertIsNone(self.turn(first)["frozen_context"])
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_messages WHERE role='user'"
                ).fetchone()["n"],
                1,
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_tool_events"
                ).fetchone()["n"],
                4,
            )
            self.assertEqual(
                c.execute("SELECT count(*) AS n FROM memory_core.conversation_requests").fetchone()[
                    "n"
                ],
                3,
            )

    def test_tool_replacement_switches_only_after_final_answer(self):
        old = self.exchange("同一句", "旧答案")
        pending = self.store.begin(body("同一句"), {})
        self.store.finish(pending, tools=[call("pending")], finish_reason="tool_calls")
        self.assertEqual(self.turn(old)["history_state"], "current")
        continuation = body("同一句")
        continuation["messages"] += [
            {"role": "assistant", "content": None, "tool_calls": [call("pending")]},
            {"role": "tool", "tool_call_id": "pending", "content": "结果"},
        ]
        record = self.store.begin(continuation, {})
        self.store.finish(record, content="新答案", finish_reason="stop")
        self.assertEqual(self.turn(old)["history_state"], "replaced")
        self.assertEqual(self.turn(pending)["history_state"], "current")

    def test_tool_protocol_mismatch_and_duplicate_requests_do_not_write(self):
        initial = body("查询")
        first = self.store.begin(initial, {"request_id": "first"})
        with self.assertRaises(DuplicateRequest) as caught:
            self.store.begin(initial, {"request_id": "first"})
        self.assertEqual(caught.exception.record["request_id"], first["request_id"])
        self.store.finish(first, tools=[call("a"), call("b")], finish_reason="tool_calls")
        bad = body("查询")
        bad["messages"] += [
            {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]},
            {"role": "tool", "tool_call_id": "a", "content": "不完整"},
        ]
        with self.assertRaises(ConversationConflict):
            self.store.begin(bad, {"request_id": "bad"})
        with connect(self.app) as c:
            self.assertEqual(
                c.execute("SELECT count(*) AS n FROM memory_core.conversation_requests").fetchone()[
                    "n"
                ],
                1,
            )

    def test_images_are_not_frozen_and_must_come_from_continuation(self):
        initial = body(
            [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET_IMAGE"}},
            ]
        )
        first = self.store.begin(initial, {})
        self.store.finish(first, tools=[call("image")], finish_reason="tool_calls")
        self.assertNotIn("SECRET_IMAGE", str(self.turn(first)))
        tail = [
            {"role": "assistant", "content": None, "tool_calls": [call("image")]},
            {"role": "tool", "tool_call_id": "image", "content": "结果"},
        ]
        with self.assertRaises(ValueError):
            self.store.begin({"model": "synthetic", "messages": tail[-1:]}, {})
        continuation = deepcopy(initial)
        continuation["messages"] += tail
        record = self.store.begin(continuation, {})
        self.assertIn("SECRET_IMAGE", str(record["upstream_body"]))

    def test_expired_round_releases_space_and_fences_late_worker(self):
        start = datetime.now(timezone.utc)
        old = self.store.begin(body("慢的一句"), {}, now=start)
        self.assertEqual(
            ConversationStore.expire_leases(self.app, now=start + timedelta(seconds=301)), 1
        )
        new = self.store.begin(body("新的一句"), {}, now=start + timedelta(seconds=302))
        self.assertFalse(
            self.store.finish(
                old, content="迟到回复", finish_reason="stop", now=start + timedelta(seconds=303)
            )
        )
        self.store.finish(
            new, content="新回复", finish_reason="stop", now=start + timedelta(seconds=304)
        )
        self.assertEqual(self.turn(old)["generation_state"], "incomplete")
        self.assertEqual(self.turn(new)["generation_state"], "complete")

    def test_parallel_new_requests_are_exclusive_but_other_spaces_work(self):
        def submit(index):
            try:
                return self.store.begin(body("请求" + str(index)), {"request_id": str(index)})
            except ConversationConflict as exc:
                self.assertEqual(exc.code, "space_busy")
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(submit, (1, 2)))
        self.assertEqual(sum(r is not None for r in records), 1)
        other = ConversationStore(self.app, "two").begin(body("另一个空间"), {})
        self.assertEqual(self.turn(other)["bot_id"], "two")

    def test_waiting_tools_timeout_and_generation_heartbeat(self):
        start = datetime.now(timezone.utc)
        record = self.store.begin(body("等工具"), {}, now=start)
        self.assertTrue(self.store.renew(record, now=start + timedelta(seconds=200)))
        self.assertEqual(
            ConversationStore.expire_leases(self.app, now=start + timedelta(seconds=301)), 0
        )
        self.store.finish(
            record,
            tools=[call("wait")],
            finish_reason="tool_calls",
            now=start + timedelta(seconds=302),
        )
        self.assertEqual(
            ConversationStore.expire_leases(self.app, now=start + timedelta(seconds=603)), 1
        )
        self.assertEqual(self.turn(record)["generation_state"], "incomplete")
        self.assertIsNone(self.turn(record)["frozen_context"])

    def test_failed_candidate_can_be_retried_without_replacing_original_early(self):
        original = self.exchange("原句", "旧答案")
        failed = self.store.begin(
            body("改过的句子"), {"operation": "edit", "target_turn_id": original["turn_id"]}
        )
        self.store.finish(failed, state="upstream_failed")
        retry = self.store.begin(
            body("改过的句子"), {"operation": "retry", "target_turn_id": failed["turn_id"]}
        )
        self.assertEqual(self.turn(original)["history_state"], "current")
        self.store.finish(retry, content="重试成功", finish_reason="stop")
        self.assertEqual(self.turn(original)["history_state"], "replaced")
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT retry_of_request_id FROM memory_core.conversation_requests WHERE id=%s",
                    (retry["request_id"],),
                ).fetchone()["retry_of_request_id"],
                failed["request_id"],
            )

    def test_empty_or_truncated_reply_does_not_replace_original(self):
        original = self.exchange("同一句", "保留的答案")
        for content, reason in (("", "stop"), ("未说完", "length")):
            candidate = self.store.begin(body("同一句"), {})
            self.store.finish(candidate, content=content, finish_reason=reason)
            self.assertEqual(self.turn(original)["history_state"], "current")
            self.assertEqual(self.turn(candidate)["generation_state"], "incomplete")

    def test_adoption_failure_rolls_back_reply_and_both_version_states(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        import psycopg

        original = self.exchange("同一句", "原来的答案")
        candidate = self.store.begin(body("同一句"), {})

        @contextmanager
        def fault_connection(dsn):
            with connect(dsn) as real:

                class FailAdoption:
                    def execute(self, query, params=None):
                        if "SET history_state='current'" in query:
                            raise psycopg.errors.RaiseException("synthetic adoption failure")
                        return real.execute(query, params)

                yield FailAdoption()

        with patch("memory_core.conversations.conversation_rounds.connect", fault_connection), self.assertRaises(
            psycopg.errors.RaiseException
        ):
            self.store.finish(candidate, content="新的答案", finish_reason="stop")
        self.assertEqual(self.turn(original)["history_state"], "current")
        self.assertEqual(self.turn(candidate)["history_state"], "candidate")
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.conversation_messages WHERE turn_id=%s AND role='assistant'",
                    (candidate["turn_id"],),
                ).fetchone()["n"],
                0,
            )
