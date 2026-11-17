"""Only actual assistant text counts as an answer; tool payloads do not."""

import unittest

from memory_core.conversations import chat_text
from memory_core.storage.config import Settings
from memory_core.conversations.conversation_history import render_turn


class HistoryAnnotationTests(unittest.TestCase):
    def render(self, state, reply=None, tools=(), content_state="complete"):
        messages = [dict(role="user", content_parts=[dict(type="text", text="合成提问")],
            occurred_at="2026-11-05T12:00:00+08:00", content_state="complete")]
        if reply is not None:
            messages.append(dict(role="assistant", content_parts=[dict(type="text", text=reply)],
                occurred_at=messages[0]["occurred_at"], content_state=content_state))
        return render_turn(dict(generation_state=state), messages, tools, Settings(3))

    def test_empty_or_whitespace_reply_only_marks_unanswered(self):
        for state in ("failed", "incomplete"):
            for reply in (None, "", " \n"):
                with self.subTest(state=state, reply=reply):
                    result = self.render(state, reply, content_state="incomplete")
                    self.assertEqual(len(result), 1)
                    self.assertEqual(result[0]["content"].count(chat_text.UNANSWERED), 1)
                    self.assertNotIn(chat_text.INCOMPLETE, str(result))

    def test_partial_reply_only_marks_incomplete(self):
        for state in ("failed", "incomplete"):
            for content_state in ("complete", "incomplete"):
                with self.subTest(state=state, content_state=content_state):
                    result = self.render(state, "半段正文", content_state=content_state)
                    self.assertEqual(str(result).count(chat_text.INCOMPLETE), 1)
                    self.assertNotIn(chat_text.UNANSWERED, str(result))

    def test_successful_reply_has_neither_marker(self):
        result = self.render("complete", "完整正文")
        self.assertNotIn(chat_text.UNANSWERED, str(result))
        self.assertNotIn(chat_text.INCOMPLETE, str(result))

    def test_tool_only_reply_does_not_count_as_assistant_body(self):
        tools = [dict(kind="call", tool_call_id="1", tool_name="天气"),
            dict(kind="result", tool_call_id="1", payload="工具返回不会变成 AI 正文")]
        for state in ("failed", "incomplete"):
            with self.subTest(state=state):
                result = self.render(state, tools=tools)
                self.assertIn(chat_text.UNANSWERED, result[0]["content"])
                self.assertNotIn(chat_text.INCOMPLETE, str(result))
                self.assertEqual(result[1]["content"], "[本轮调用了工具：天气（已返回）]")

    def test_tool_preamble_is_a_reply_even_when_the_next_step_has_no_body(self):
        tools = [dict(kind="call", tool_call_id="1", tool_name="天气")]
        result = self.render("incomplete", "先查一下", tools=tools)
        self.assertNotIn(chat_text.UNANSWERED, str(result))
        self.assertEqual(str(result).count(chat_text.INCOMPLETE), 1)
        self.assertIn("天气（未返回）", result[-1]["content"])

    def test_tool_status_uses_result_presence_and_matches_each_call(self):
        tools = []
        for index, status in enumerate(("success", "failure", "unknown", None)):
            tools.append(dict(kind="call", tool_call_id=str(index), tool_name="工具" + str(index)))
            # The last tool has no result; other payloads need no status.
            if status is not None:
                tools.append(dict(kind="result", tool_call_id=str(index), reported_status=status))
        result = self.render("complete", "完整正文", tools=tools)
        self.assertIn("[本轮调用了工具：工具0（已返回）、工具1（已返回）、工具2（已返回）、工具3（未返回）]", result[-1]["content"])
