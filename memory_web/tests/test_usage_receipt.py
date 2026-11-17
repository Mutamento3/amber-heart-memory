import json
import unittest

from memory_web.proxy.usage_receipt import UsageFilter, StreamReceipt, clean_response, encode


class UsageReceiptTests(unittest.TestCase):
    def test_every_character_boundary_and_repeated_tags(self):
        raw = "正常 < 是文字\n正文\n<used>1,３,3,99</used>\n<USED>2</USED>  "
        for index in range(len(raw) + 1):
            with self.subTest(index=index):
                parser = UsageFilter()
                clean = (
                    parser.feed(raw[:index])
                    + parser.feed(raw[index:])
                    + parser.finish()
                )
                self.assertEqual(clean, "正常 < 是文字\n正文")
                self.assertEqual(parser.reported, [1, 2, 3])

    def test_one_character_chunks(self):
        parser = UsageFilter()
        clean = (
            "".join(parser.feed(c) for c in "你好\n<used>1,1,4</used>")
            + parser.finish()
        )
        self.assertEqual((clean, parser.reported), ("你好", [1, 4]))

    def test_truncation_never_leaks_control_text_or_counts(self):
        raw = "<used>1,2</used>"
        for length in range(1, len(raw)):
            with self.subTest(length=length):
                parser = UsageFilter()
                self.assertEqual(
                    parser.feed("正文\n" + raw[:length]) + parser.finish(), "正文"
                )
                self.assertEqual(parser.reported, [])

    def test_bad_or_nonterminal_reports_are_stripped_but_not_counted(self):
        for raw in (
            "<used>one</used>",
            "<used>-1</used>",
            "<used>1.5</used>",
            "<used>" + "1" * 300 + "</used>",
        ):
            parser = UsageFilter()
            self.assertEqual(parser.feed("正文\n" + raw) + parser.finish(), "正文")
            self.assertEqual(parser.reported, [])
        parser = UsageFilter()
        self.assertEqual(parser.feed("甲<used>1</used>乙") + parser.finish(), "甲乙")
        self.assertEqual(parser.reported, [])

    def test_plain_text_whitespace_and_non_tag_markup_remain_identical(self):
        for raw in ("正常空格  \n", "x < y", "<user>hello</user>", ""):
            parser = UsageFilter()
            self.assertEqual(parser.feed(raw) + parser.finish(), raw)

    def test_nonstream_and_stream_clean_content_identically_with_combined_finish(self):
        raw = "正文\n<used>1</used>"
        clean, reported, reported_none = clean_response(
            {"choices": [{"index": 0, "message": {"content": raw}}]}
        )
        parser = StreamReceipt()
        delta = {
            "id": "test",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": raw, "reasoning_content": "合成思考"},
                    "finish_reason": "stop",
                }
            ],
        }
        frames = parser.frames(encode(delta), delta)
        value = json.loads(frames[0][6:])
        self.assertEqual(
            value["choices"][0]["delta"]["content"],
            clean["choices"][0]["message"]["content"],
        )
        self.assertEqual(value["choices"][0]["delta"]["reasoning_content"], "合成思考")
        self.assertEqual(parser.filter.reported, reported)
        self.assertEqual(parser.filter.reported_none, reported_none)

    def test_none_report_split_repeat_and_truncation(self):
        raw = "正文\n<used>无</used>\n<used> 无 </used>  "
        for index in range(len(raw) + 1):
            with self.subTest(index=index):
                parser = UsageFilter()
                self.assertEqual(parser.feed(raw[:index]) + parser.feed(raw[index:]) + parser.finish(), "正文")
                self.assertEqual(parser.reported, [])
                self.assertTrue(parser.reported_none)
        tag = "<used>无</used>"
        for index in range(1, len(tag)):
            parser = UsageFilter()
            self.assertEqual(parser.feed("正文" + tag[:index]) + parser.finish(), "正文")
            self.assertFalse(parser.reported_none)

    def test_none_conflicting_invalid_or_nonterminal_never_claims_unused(self):
        for raw in ("<used>无</used><used>1</used>", "<used>无</used><used>99</used>",
                    "<used>无</used><used>one</used>", "<used>无</used>接着说正文", "普通正文"):
            with self.subTest(raw=raw):
                parser = UsageFilter()
                parser.feed(raw)
                parser.finish()
                self.assertFalse(parser.reported_none)

    def test_none_nonstream_and_stream_have_same_explicit_receipt(self):
        raw = "正文\n<used>无</used>"
        clean, reported, reported_none = clean_response({"choices": [{"message": {"content": raw}}]})
        parser = StreamReceipt()
        visible = []
        for char in raw:
            data = {"choices": [{"index": 0, "delta": {"content": char}, "finish_reason": None}]}
            visible += parser.frames(encode(data), data)
        self.assertEqual(''.join(json.loads(frame[6:])["choices"][0]["delta"]["content"] for frame in visible),
                         clean["choices"][0]["message"]["content"])
        self.assertEqual(parser.filter.reported, reported)
        self.assertTrue(reported_none)
        self.assertTrue(parser.filter.reported_none)
