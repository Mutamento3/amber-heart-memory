from dataclasses import replace
import unittest

from memory_core.daily.summary_policy import SummarySettings, protected_tail, summary_batch


class SummaryPolicyTests(unittest.TestCase):
    def test_counts_are_rounds_and_delayed_jobs_consume_fixed_cycle(self):
        settings = SummarySettings()
        self.assertEqual(summary_batch(list(range(13)), settings, characters=20000), [])
        self.assertEqual(
            summary_batch(list(range(14)), settings, characters=100), list(range(12))
        )
        self.assertEqual(
            summary_batch(list(range(16)), settings, characters=100), list(range(12))
        )

    def test_length_triggers_early_but_never_consumes_the_relay(self):
        settings = SummarySettings()
        self.assertEqual(
            summary_batch(list(range(4)), settings, characters=30000), []
        )
        self.assertEqual(
            summary_batch(list(range(4)), settings, characters=30001), [0, 1]
        )
        self.assertEqual(summary_batch(list(range(2)), settings, characters=30001), [])

    def test_protection_keeps_whole_contiguous_suffix_and_records_omissions(self):
        groups = [
            {"turn_id": i, "messages": [{"role": "user", "content": "x" * n}]}
            for i, n in enumerate((4, 7, 3))
        ]
        kept, omitted = protected_tail(groups, 10)
        self.assertEqual([g["turn_id"] for g in kept], [1, 2])
        self.assertEqual([g["turn_id"] for g in omitted], [0])
        kept, omitted = protected_tail(groups, 2)
        self.assertEqual(kept, [])
        self.assertEqual(omitted, groups)

    def test_invalid_thresholds_fail_before_any_database_or_model(self):
        settings = SummarySettings()
        for field, value in (
            ("trigger_messages", 27),
            ("trigger_messages", 4),
            ("retain_turns", 0),
            ("rollup_trigger", 2),
            ("max_history_chars", 0),
            ("retain_turns", True),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                replace(settings, **{field: value})
