"""Fake HTTP only: role budgets reach the actual transport and remain absolute."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

from memory_core.models.completion_model import CompletionModelError
from memory_core.testing.provider_server import ProviderServer
from memory_web.config import WebSettings
from memory_web.models.diary_model import DiaryModel
from memory_web.models.extraction_model import ExtractionModel
from memory_web.models.summary_model import SummaryModel
from memory_web.models.topic_summary_model import TopicSummaryModel


REPLY = dict(choices=[dict(message=dict(content="合成结果", reasoning_content="不存储的合成思考"),
    finish_reason="stop")], usage=dict(completion_tokens=7))
DIARY_TEXT = "合成日记描述这一天的安排。" * 10


class RoleTimeoutTests(unittest.TestCase):
    def settings(self, **kwargs):
        return WebSettings("synthetic", "synthetic-owner", "https://memory.example.test",
            Path("/tmp/synthetic.hash"), access_key_master="synthetic-master",
            upstream_base_url="https://upstream.example.test/v1", upstream_api_key="synthetic-key",
            summary_model="synthetic-summary", diary_model="synthetic-diary",
            extraction_model="synthetic-extractor", topic_summary_model="synthetic-topic", **kwargs)

    def env(self):
        return dict(MEMORY_DSN="synthetic", MEMORY_CONFIG_DSN="synthetic-owner",
            MEMORY_WEB_ORIGIN="https://memory.example.test", MEMORY_WEB_PASSWORD_FILE="/tmp/synthetic.hash")

    def test_defaults_and_env_overrides_leave_recall_limits_unchanged(self):
        with patch.dict("os.environ", self.env(), clear=True):
            defaults = WebSettings.from_env()
        expected = dict(summary_timeout_seconds=120, diary_timeout_seconds=300,
            extraction_timeout_seconds=600, topic_summary_timeout_seconds=300)
        for field, value in expected.items():
            self.assertEqual(getattr(defaults, field), value)
        env = self.env() | {"MEMORY_" + field.upper(): str(index + 0.5)
            for index, field in enumerate(expected, 1)}
        with patch.dict("os.environ", env, clear=True):
            configured = WebSettings.from_env()
        for index, field in enumerate(expected, 1):
            self.assertEqual(getattr(configured, field), index + 0.5)
        self.assertEqual(configured.recall_timeout_seconds, 8)
        self.assertEqual(configured.recall_models.timeout_seconds, 5)

    def test_import_concurrency_defaults_to_three_and_requires_a_positive_integer(self):
        with patch.dict('os.environ', self.env(), clear=True):
            self.assertEqual(WebSettings.from_env().import_concurrency, 3)
        with patch.dict('os.environ', self.env() | {'MEMORY_IMPORT_CONCURRENCY': '1'}, clear=True):
            self.assertEqual(WebSettings.from_env().import_concurrency, 1)
        for value in ('0', '-1', '1.5', ''):
            with self.subTest(value=value), patch.dict('os.environ',
                    self.env() | {'MEMORY_IMPORT_CONCURRENCY': value}, clear=True):
                with self.assertRaisesRegex(ValueError, 'MEMORY_IMPORT_CONCURRENCY'):
                    WebSettings.from_env()

    def test_invalid_timeouts_fail_at_configuration_without_echoing_values(self):
        for role in ("summary", "diary", "extraction", "topic_summary"):
            field = role + "_timeout_seconds"
            for value in (True, None, 0, -1, float("inf"), float("nan"), "300"):
                with self.subTest(role=role, value=value), self.assertRaises(ValueError):
                    replace(self.settings(), **{field: value})
            env_name = "MEMORY_" + field.upper()
            for value in ("", "private-invalid-value", "inf", "nan", "0", "-1"):
                with self.subTest(role=role, env=value), patch.dict("os.environ",
                        self.env() | {env_name: value}, clear=True):
                    with self.assertRaises(ValueError) as error:
                        WebSettings.from_env()
                    self.assertIn(env_name, str(error.exception))
                    self.assertNotIn("private-invalid-value", str(error.exception))

    def call_roles(self, settings, transport=None):
        result = [asyncio.run(SummaryModel(settings, transport=transport).summarize("规则", "材料")),
            asyncio.run(DiaryModel(settings, transport=transport).generate("规则", "材料"))]
        records = []
        extractor = ExtractionModel(settings, transport=transport)
        result.extend(extractor.stage(stage, records.append).extract("合成规则和材料")
            for stage in ("selection", "structure"))
        result.append(TopicSummaryModel(settings, transport=transport).generate("合成规则和材料", records.append))
        return result, records

    def test_each_role_passes_its_default_and_custom_budget_to_http(self):
        for settings in (self.settings(), self.settings(summary_timeout_seconds=131,
                diary_timeout_seconds=301, extraction_timeout_seconds=302, topic_summary_timeout_seconds=303)):
            sent = []
            def handler(request):
                sent.append(request.extensions["timeout"]["read"])
                if json.loads(request.content)["model"] == settings.diary_model:
                    return httpx.Response(200, json=dict(choices=[
                        dict(message=dict(content=DIARY_TEXT), finish_reason="stop")
                    ]))
                return httpx.Response(200, json=REPLY)
            result, records = self.call_roles(settings, httpx.MockTransport(handler))
            self.assertEqual(sent, [settings.summary_timeout_seconds, settings.diary_timeout_seconds,
                settings.extraction_timeout_seconds, settings.extraction_timeout_seconds,
                settings.topic_summary_timeout_seconds])
            self.assertNotIn("合成思考", str(result) + str(records))

    def test_default_adapters_without_injected_transport_only_call_fake_http(self):
        with ProviderServer() as server:
            settings = replace(self.settings(), upstream_base_url=server.url)
            server.replies[settings.diary_model] = DIARY_TEXT
            result, records = self.call_roles(settings)
            self.assertEqual(len(server.requests), 5)
            self.assertTrue(all(r["authorization"] == "Bearer synthetic-key" for r in server.requests))
            self.assertEqual([r["stage"] for r in records], ["selection", "structure", "topic_summary"])
            self.assertNotIn("synthetic-key", str(result) + str(records))

    def test_slow_fake_thinking_uses_budget_and_safe_failure_for_both_extraction_steps(self):
        async def slow(request):
            await asyncio.sleep(0.08)
            return httpx.Response(200, json=REPLY)
        for stage in ("selection", "structure"):
            records = []
            short = ExtractionModel(self.settings(extraction_timeout_seconds=0.02), transport=httpx.MockTransport(slow))
            with self.subTest(stage=stage), self.assertRaisesRegex(CompletionModelError, stage + "_timeout"):
                short.stage(stage, records.append).extract("不存入错误的合成材料")
            self.assertEqual(records[0]["state"], "failed")
            self.assertNotIn("合成材料", str(records))
            longer = ExtractionModel(self.settings(extraction_timeout_seconds=0.5), transport=httpx.MockTransport(slow))
            self.assertEqual(longer.stage(stage, records.append).extract("合成材料"), "合成结果")
            self.assertEqual(records[-1]["state"], "succeeded")

    def test_diary_and_topic_summary_timeouts_still_raise_safe_role_errors(self):
        async def slow(request):
            await asyncio.sleep(0.1)
            return httpx.Response(200, json=REPLY)
        transport = httpx.MockTransport(slow)
        settings = self.settings(diary_timeout_seconds=0.02, topic_summary_timeout_seconds=0.02)
        with self.assertRaisesRegex(CompletionModelError, "diary_timeout"):
            asyncio.run(DiaryModel(settings, transport=transport).generate("规则", "材料"))
        records = []
        with self.assertRaisesRegex(CompletionModelError, "topic_summary_timeout"):
            TopicSummaryModel(settings, transport=transport).generate("材料", records.append)
        self.assertEqual(records[0]["code"], "topic_summary_timeout")

    def test_partial_http_chunks_do_not_reset_the_summary_budget(self):
        class ThinkingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                raw = json.dumps(REPLY).encode()
                for index in range(0, len(raw), 10):
                    await asyncio.sleep(0.01)
                    yield raw[index:index + 10]
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=ThinkingBody()))
        with self.assertRaisesRegex(CompletionModelError, "summary_timeout"):
            asyncio.run(SummaryModel(self.settings(summary_timeout_seconds=0.04), transport=transport).summarize("规则", "材料"))

    def test_summary_backup_uses_the_same_configured_budget_after_timeout(self):
        sent = []
        async def handler(request):
            sent.append((json.loads(request.content)["model"], request.extensions["timeout"]["read"]))
            if len(sent) <= 2:
                await asyncio.sleep(0.1)
            return httpx.Response(200, json=REPLY)
        settings = replace(self.settings(summary_timeout_seconds=0.02), summary_backup_model="synthetic-backup")
        result = asyncio.run(SummaryModel(settings, transport=httpx.MockTransport(handler)).summarize("规则", "材料"))
        self.assertEqual(sent, [("synthetic-summary", 0.02), ("synthetic-summary", 0.02),
                                ("synthetic-backup", 0.02)])
        self.assertEqual(result["model"], "synthetic-backup")
