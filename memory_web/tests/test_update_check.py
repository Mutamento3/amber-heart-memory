"""Update notice: only a newer second version number, cached, never raising."""

import time
import unittest
from unittest.mock import patch

from memory_web.admin import update_check
from memory_web.admin.update_check import UpdateChecker


class UpdateCheckTests(unittest.TestCase):
    def checker(self, tags, current="0.4.1"):
        clock = [1000.0]
        with patch.object(update_check, "VERSION", current):
            checker = UpdateChecker(enabled=True, fetch=lambda: tags, fetch_notes=lambda version: f"v{version} 中文说明\n- 新功能\n\n---\nEnglish", clock=lambda: clock[0])
        return checker, clock

    def settle(self, checker):
        checker.status()
        for _ in range(100):
            if not checker.running:
                return
            time.sleep(0.01)

    def test_only_a_newer_feature_version_is_announced(self):
        checker, _ = self.checker(["v0.4.3", "v0.4.2", "not-a-version"])
        self.settle(checker)
        self.assertIsNone(checker.status())
        checker, _ = self.checker(["v0.4.3", "v0.5.0", "v0.10.0"])
        self.settle(checker)
        with patch.object(update_check, "VERSION", "0.4.1"):
            status = checker.status()
        self.assertEqual(status["latest"], "0.10.0")
        self.assertEqual(status["notes"], "v0.10.0 中文说明\n- 新功能")

    def test_missing_release_note_still_announces(self):
        def missing(version):
            raise OSError("no release")
        with patch.object(update_check, "VERSION", "0.4.1"):
            checker = UpdateChecker(enabled=True, fetch=lambda: ["v0.5.0"], fetch_notes=missing)
            self.settle(checker)
            status = checker.status()
        self.assertEqual((status["latest"], status["notes"]), ("0.5.0", None))

    def test_failure_is_silent_and_retried_later(self):
        calls = []
        def broken():
            calls.append(1)
            raise OSError("unreachable")
        clock = [0.0]
        checker = UpdateChecker(enabled=True, fetch=broken, clock=lambda: clock[0])
        self.settle(checker)
        self.assertIsNone(checker.status())
        self.assertEqual(len(calls), 1)
        clock[0] += update_check.RETRY_SECONDS + 1
        self.settle(checker)
        self.assertEqual(len(calls), 2)

    def test_disabled_never_fetches(self):
        checker = UpdateChecker(enabled=False, fetch=lambda: self.fail("fetched"))
        self.assertIsNone(checker.status())


if __name__ == "__main__":
    unittest.main()
