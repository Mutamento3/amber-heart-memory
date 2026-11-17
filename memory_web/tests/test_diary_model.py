import asyncio
from dataclasses import replace
import json
from pathlib import Path
import unittest

import httpx

from memory_web.config import WebSettings
from memory_web.diary_model import DiaryModel, DiaryModelError

DIARY_TEXT = "合成日记" * 25


class DiaryModelTests(unittest.TestCase):
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

    def test_diary_role_uses_its_model_and_inherits_main_endpoint(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            self.assertEqual(
                str(request.url), "https://upstream.example.test/v1/chat/completions"
            )
            self.assertEqual(
                request.headers["authorization"], "Bearer synthetic-main-key"
            )
            return httpx.Response(
                200,
                json=dict(
                    choices=[
                        dict(
                            message=dict(
                                content=DIARY_TEXT, reasoning_content="不保留的思考"
                            ),
                            finish_reason="stop",
                        )
                    ]
                ),
            )

        adapter = DiaryModel(
            self.settings(diary_model="synthetic-diary"),
            transport=httpx.MockTransport(handler),
        )
        result = asyncio.run(adapter.generate("中性日记规则", "合成正文"))
        self.assertEqual(calls[0]["model"], "synthetic-diary")
        self.assertEqual(
            result, dict(content=DIARY_TEXT, model="synthetic-diary", usage=None)
        )
        with self.assertRaisesRegex(DiaryModelError, "diary_model_unconfigured"):
            asyncio.run(
                DiaryModel(
                    self.settings(), transport=httpx.MockTransport(handler)
                ).generate("规则", "正文")
            )
        self.assertEqual(len(calls), 1)

    def test_backup_only_after_failure_and_error_text_never_escapes(self):
        calls = []

        def handler(request):
            model = json.loads(request.content)["model"]
            calls.append(model)
            if model == "primary":
                return httpx.Response(
                    503, json=dict(error=dict(message="synthetic-secret-key"))
                )
            return httpx.Response(
                200,
                json=dict(
                    choices=[
                        dict(message=dict(content=DIARY_TEXT), finish_reason="stop")
                    ]
                ),
            )

        adapter = DiaryModel(
            self.settings(diary_model="primary", diary_backup_model="backup"),
            transport=httpx.MockTransport(handler),
        )
        result = asyncio.run(adapter.generate("规则", "正文"))
        self.assertEqual(calls, ["primary", "backup"])
        self.assertEqual(result["model"], "backup")
        self.assertNotIn("synthetic-secret-key", str(result))

    def test_short_or_refusal_text_tries_backup_before_publishing(self):
        for bad in (DIARY_TEXT[:-1], DIARY_TEXT + "无法提供", "I cannot " + DIARY_TEXT):
            with self.subTest(bad_length=len(bad)):
                calls = []

                def handler(request):
                    model = json.loads(request.content)["model"]
                    calls.append(model)
                    content = bad if model == "primary" else DIARY_TEXT
                    return httpx.Response(200, json=dict(choices=[
                        dict(message=dict(content=content), finish_reason="stop")
                    ]))

                adapter = DiaryModel(
                    self.settings(diary_model="primary", diary_backup_model="backup"),
                    transport=httpx.MockTransport(handler),
                )
                result = asyncio.run(adapter.generate("规则", "正文"))
                self.assertEqual(calls, ["primary", "backup"])
                self.assertEqual(result["content"], DIARY_TEXT)

    def test_dedicated_configuration_is_paired_and_keys_are_not_in_repr(self):
        settings = self.settings(
            diary_model="synthetic",
            diary_base_url="https://diary.example.test/v1",
            diary_api_key="synthetic-secret-key",
        )
        self.assertNotIn("synthetic-secret-key", repr(settings))
        for patch in (
            dict(diary_api_key=None),
            dict(diary_base_url="https://user:password@diary.example.test/v1"),
            dict(diary_model=None, diary_backup_model="backup"),
        ):
            with self.assertRaises(ValueError):
                replace(settings, **patch)
