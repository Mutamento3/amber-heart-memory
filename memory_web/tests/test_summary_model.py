import asyncio
from dataclasses import replace
import json
from pathlib import Path
import unittest

import httpx

from memory_web.config import WebSettings
from memory_web.models.summary_model import SummaryModel, SummaryModelError


class SummaryModelTests(unittest.TestCase):
    def settings(self, **extra):
        return WebSettings(
            "synthetic",
            "owner",
            "https://memory.example.test",
            Path("/tmp/synthetic.hash"),
            access_key_master="synthetic-master",
            upstream_base_url="https://upstream.example.test/v1",
            upstream_api_key="synthetic-main-key",
            **extra
        )

    def test_model_is_explicit_and_default_endpoint_reuses_main_upstream(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(
                request.url, "https://upstream.example.test/v1/chat/completions"
            )
            self.assertEqual(
                request.headers["authorization"], "Bearer synthetic-main-key"
            )
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "synthetic-summary")
            self.assertFalse(payload["stream"])
            self.assertEqual(
                payload["messages"],
                [
                    {"role": "system", "content": "公开提示词"},
                    {"role": "user", "content": "合成正文"},
                ],
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "合成摘要",
                                "reasoning_content": "不保存的合成思考",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 3},
                },
            )

        settings = self.settings(summary_model="synthetic-summary")
        result = asyncio.run(
            SummaryModel(settings, transport=httpx.MockTransport(handler)).summarize(
                "公开提示词", "合成正文"
            )
        )
        self.assertEqual(
            result,
            dict(
                content="合成摘要", model="synthetic-summary", usage={"total_tokens": 3}
            ),
        )
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(SummaryModelError, "unconfigured"):
            asyncio.run(
                SummaryModel(
                    self.settings(), transport=httpx.MockTransport(handler)
                ).summarize("提示词", "正文")
            )
        self.assertEqual(len(calls), 1)

    def test_backup_only_after_failure_and_never_preserves_provider_secret(self):
        calls = []

        def handler(request):
            model = json.loads(request.content)["model"]
            calls.append(model)
            if model == "synthetic-primary":
                return httpx.Response(
                    503,
                    json={
                        "error": {
                            "message": "synthetic-main-key should never be persisted"
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "备用合成摘要"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        settings = self.settings(
            summary_model="synthetic-primary", summary_backup_model="synthetic-backup"
        )
        result = asyncio.run(
            SummaryModel(settings, transport=httpx.MockTransport(handler)).summarize(
                "提示词", "正文"
            )
        )
        self.assertEqual(calls, ["synthetic-primary", "synthetic-primary", "synthetic-backup"])
        self.assertEqual(result["model"], "synthetic-backup")
        self.assertNotIn("synthetic-main-key", str(result))

    def test_empty_truncated_and_invalid_responses_are_rejected(self):
        for payload in (
            {"error": {"message": "synthetic-main-key"}},
            [],
            {"choices": []},
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": "半段"}, "finish_reason": "length"}]},
        ):
            with self.subTest(payload=payload):
                model = SummaryModel(
                    self.settings(summary_model="synthetic-summary"),
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=payload)
                    ),
                )
                with self.assertRaises(SummaryModelError) as error:
                    asyncio.run(model.summarize("提示词", "正文"))
                self.assertNotIn("synthetic-main-key", str(error.exception))

    def test_dedicated_upstream_validation_and_key_repr(self):
        settings = self.settings(
            summary_model="synthetic-summary",
            summary_base_url="https://summary.example.test/v1",
            summary_api_key="synthetic-summary-key",
        )
        self.assertNotIn("synthetic-summary-key", repr(settings))
        with self.assertRaises(ValueError):
            replace(settings, summary_api_key=None)
        with self.assertRaises(ValueError):
            replace(
                settings,
                summary_base_url="https://user:password@summary.example.test/v1",
            )
        with self.assertRaises(ValueError):
            replace(
                settings, summary_model=None, summary_backup_model="synthetic-backup"
            )
