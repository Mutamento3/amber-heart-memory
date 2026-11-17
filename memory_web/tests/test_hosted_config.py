from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from memory_web.config import WebSettings


class HostedConfigTests(unittest.TestCase):
    def test_document_rerank_format_is_validated_on_startup(self):
        environ = dict(
            MEMORY_DSN="test",
            MEMORY_CONFIG_DSN="owner",
            MEMORY_WEB_ORIGIN="https://memory.example.test",
            MEMORY_WEB_PASSWORD_FILE="/tmp/synthetic.hash",
        )
        for value in ("", "auto"):
            with self.subTest(value=value), patch.dict(
                "os.environ", dict(environ, MEMORY_DOCUMENT_RERANK_FORMAT=value), clear=True
            ):
                with self.assertRaisesRegex(ValueError, "MEMORY_DOCUMENT_RERANK_FORMAT"):
                    WebSettings.from_env()

    def test_defaults_env_overrides_and_invalid_values(self):
        settings = WebSettings(
            "test", "owner", "https://memory.example.test", Path("/tmp/synthetic.hash")
        )
        self.assertEqual(
            (settings.recall_timeout_seconds, settings.previous_day_messages), (8, 4)
        )
        environ = dict(
            MEMORY_DSN="test",
            MEMORY_CONFIG_DSN="owner",
            MEMORY_WEB_ORIGIN=settings.origin,
            MEMORY_WEB_PASSWORD_FILE="/tmp/synthetic.hash",
            MEMORY_PROXY_RECALL_TIMEOUT_SECONDS="0.25",
            MEMORY_PROXY_PREVIOUS_DAY_MESSAGES="0",
        )
        with patch.dict("os.environ", environ, clear=True):
            actual = WebSettings.from_env()
            self.assertEqual(
                (actual.recall_timeout_seconds, actual.previous_day_messages), (0.25, 0)
            )
        for value in (0, -1, True, float("nan"), float("inf"), "8"):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                replace(settings, recall_timeout_seconds=value)
        for value in (-1, 1001, True, 4.1, "4"):
            with self.subTest(previous=value), self.assertRaises(ValueError):
                replace(settings, previous_day_messages=value)
