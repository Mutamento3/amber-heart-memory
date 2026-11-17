from datetime import date, datetime
import unittest

from memory_core.config import Settings
from memory_core.daily_policy import DailySettings


class DailyPolicyTests(unittest.TestCase):
    def test_before_and_after_schedule_only_finish_elapsed_business_days(self):
        policy, core = DailySettings(), Settings(3, "Asia/Shanghai", 3)
        before = datetime.fromisoformat("2026-11-03T03:14:59+08:00")
        after = datetime.fromisoformat("2026-11-03T03:15:00+08:00")
        self.assertEqual(policy.due_date(before, core), date(2026, 11, 2))
        self.assertEqual(
            policy.diary_range(after, core), (date(2026, 10, 4), date(2026, 11, 2))
        )

    def test_custom_timezone_boundary_time_and_backfill(self):
        core = Settings(3, "America/New_York", 4)
        policy = DailySettings(backfill_days=2, run_time="05:30")
        now = datetime.fromisoformat("2026-11-03T10:30:00+00:00")
        self.assertEqual(
            policy.diary_range(now, core), (date(2026, 11, 1), date(2026, 11, 2))
        )
        with self.assertRaisesRegex(ValueError, "分界之后"):
            DailySettings(run_time="04:00").scheduled_at(date(2026, 11, 3), core)

    def test_daylight_saving_missing_and_repeated_hours_share_one_date_key(self):
        core = Settings(3, "America/New_York", 0)
        policy = DailySettings(run_time="02:30")
        self.assertEqual(policy.scheduled_at(date(2026, 3, 8), core).hour, 3)
        repeated = DailySettings(run_time="01:30")
        for offset in ("-04:00", "-05:00"):
            now = datetime.fromisoformat("2026-11-01T01:45:00" + offset)
            self.assertEqual(repeated.due_date(now, core), date(2026, 11, 1))

    def test_extraction_range_has_its_own_bounded_backfill_and_shared_clock(self):
        core = Settings(3, "America/New_York", 4)
        now = datetime.fromisoformat("2026-11-03T10:30:00+00:00")
        policy = DailySettings(
            backfill_days=1, extraction_backfill_days=2, run_time="05:30"
        )
        self.assertEqual(
            policy.diary_range(now, core), (date(2026, 11, 2), date(2026, 11, 2))
        )
        self.assertEqual(
            policy.extraction_range(now, core), (date(2026, 11, 1), date(2026, 11, 2))
        )
        self.assertEqual(DailySettings().extraction_backfill_days, 30)
        for invalid in (0, -1, 31, True, "2"):
            with self.assertRaises(ValueError):
                DailySettings(extraction_backfill_days=invalid)
