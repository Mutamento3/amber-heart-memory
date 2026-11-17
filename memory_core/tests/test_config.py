import os
from datetime import date, datetime, timezone
import subprocess
import sys
import unittest

from memory_core.clock import business_day_bounds, business_day_label, format_timestamp
from memory_core.config import Settings


class ConfigTests(unittest.TestCase):
    def test_vector_dimensions_limits(self):
        for value in (1, 768, 1536, 2000):
            self.assertEqual(Settings(value).dimensions, value)
        for value in (0, -1, 2001, 3072, 1.5, True, "1536"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "2000"):
                Settings(value)

    def test_invalid_timezone_and_hour(self):
        for zone in ("invalid/zone", "", "../UTC"):
            with self.assertRaises(ValueError):
                Settings(timezone=zone)
        for hour in (-1, 24, True, 3.5):
            with self.assertRaises(ValueError):
                Settings(day_start_hour=hour)

    def test_default_boundary(self):
        settings = Settings()
        self.assertEqual(business_day_label(datetime.fromisoformat("2026-06-12T02:59:59+08:00"), settings), date(2026, 6, 11))
        self.assertEqual(business_day_label(datetime.fromisoformat("2026-06-12T03:00:00+08:00"), settings), date(2026, 6, 12))

    def test_other_timezone_and_boundary(self):
        settings = Settings(timezone="America/New_York", day_start_hour=5)
        before = datetime.fromisoformat("2026-06-12T08:59:59+00:00")
        after = datetime.fromisoformat("2026-06-12T09:00:00+00:00")
        self.assertEqual(business_day_label(before, settings), date(2026, 6, 11))
        self.assertEqual(business_day_label(after, settings), date(2026, 6, 12))
        self.assertEqual(format_timestamp(after, settings), "2026-06-12T05:00:00-04:00")

    def test_different_offsets_mean_same_instant(self):
        settings = Settings()
        a = datetime.fromisoformat("2026-06-12T03:00:00+08:00")
        b = datetime.fromisoformat("2026-06-11T19:00:00+00:00")
        self.assertEqual(format_timestamp(a, settings), format_timestamp(b, settings))
        self.assertEqual(business_day_label(a, settings), business_day_label(b, settings))
        with self.assertRaises(ValueError):
            format_timestamp(datetime(2026, 6, 12), settings)

    def test_dst_day_lengths_and_ambiguous_boundary(self):
        settings = Settings(timezone="America/New_York", day_start_hour=0)
        for label, hours in ((date(2026, 3, 8), 23), (date(2026, 11, 1), 25)):
            start, end = business_day_bounds(label, settings)
            self.assertEqual((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds(), hours * 3600)
        settings = Settings(timezone="America/New_York", day_start_hour=1)
        repeated = datetime.fromisoformat("2026-11-01T01:30:00-05:00")
        self.assertEqual(business_day_label(repeated, settings), date(2026, 11, 1))
        start, _ = business_day_bounds(date(2026, 3, 8), Settings(timezone="America/New_York", day_start_hour=2))
        self.assertEqual(start.isoformat(), "2026-03-08T03:00:00-04:00")

    def test_cli_rejects_large_dimensions_before_credentials_or_connection(self):
        env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-m", "memory_core.install", "--dimensions", "2001"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("HNSW", result.stderr)
        self.assertIn("2000", result.stderr)
        self.assertNotIn("连接串", result.stderr)


if __name__ == "__main__":
    unittest.main()
