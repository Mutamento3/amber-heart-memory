"""Memory-only reasoning restore for tool continuations."""

import unittest

from memory_web.proxy.proxy import Accumulator
from memory_web.proxy import reasoning_cache
from memory_web.proxy.reasoning_cache import ReasoningCache


class ReasoningCacheTests(unittest.TestCase):
    def test_stream_deltas_collect_reasoning_beside_the_archived_text(self):
        data = Accumulator()
        data.consume({"choices": [{"index": 0, "delta": {"reasoning_content": "先想"}}]})
        data.consume({"choices": [{"index": 0, "delta": {"reasoning_content": "一下", "content": "好"}}]})
        self.assertEqual("".join(data.reasoning), "先想一下")
        self.assertEqual(data.result()["content"], "好")
        self.assertNotIn("reasoning", data.result())

    def test_fills_only_missing_messages_of_the_current_round_and_expires(self):
        clock = [0.0]
        cache = ReasoningCache(clock=lambda: clock[0])
        cache.remember("s", 7, 0, "第一次")
        cache.remember("s", 7, 1, "第二次")
        call = lambda **extra: {"role": "assistant", "tool_calls": [{"id": "x"}], **extra}
        messages = [call(), {"role": "user", "content": "新问题"}, call(), {"role": "tool"}, call(reasoning_content="自带"), {"role": "tool"}]
        self.assertEqual(cache.fill("s", 7, 2, messages), 1)
        self.assertNotIn("reasoning_content", messages[0])
        self.assertEqual((messages[2]["reasoning_content"], messages[4]["reasoning_content"]), ("第一次", "自带"))
        self.assertEqual(cache.fill("other", 7, 2, [call()]), 0)
        clock[0] += reasoning_cache.TTL_SECONDS + 1
        late = [{"role": "user"}, call(), {"role": "tool"}]
        self.assertEqual(cache.fill("s", 7, 1, late), 0)


if __name__ == "__main__":
    unittest.main()
