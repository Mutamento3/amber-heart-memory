from dataclasses import replace
import json
from pathlib import Path
import unittest

import httpx

from memory_core.models.completion_model import CompletionModelError
from memory_web.config import WebSettings
from memory_web.models.extraction_model import ExtractionModel, safe_usage


class ExtractionModelTests(unittest.TestCase):
    def settings(self, **extra):
        return WebSettings(
            "synthetic",
            "synthetic-owner",
            "https://memory.example.test",
            Path("/tmp/synthetic.hash"),
            access_key_master="synthetic-master",
            upstream_base_url="https://upstream.example.test/v1",
            upstream_api_key="synthetic-key",
            **extra,
        )

    def test_missing_model_skips_and_dedicated_settings_are_paired(self):
        adapter = ExtractionModel(self.settings())
        self.assertFalse(adapter.configured)
        settings = self.settings(extraction_model="synthetic-extractor")
        self.assertTrue(ExtractionModel(settings).configured)
        with self.assertRaises(ValueError):
            replace(settings, extraction_api_key="synthetic-secret")
        self.assertNotIn(
            "synthetic-secret",
            repr(
                replace(
                    settings,
                    extraction_base_url="https://extract.example.test/v1",
                    extraction_api_key="synthetic-secret",
                )
            ),
        )

    def test_two_steps_preserve_existing_prompt_and_record_only_usage(self):
        sent, records = [], []

        def handler(request):
            sent.append(json.loads(request.content))
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-key")
            return httpx.Response(
                200,
                json=dict(
                    choices=[
                        dict(
                            message=dict(content='{"synthetic":true}'),
                            finish_reason="stop",
                        )
                    ],
                    usage=dict(
                        prompt_tokens=10,
                        completion_tokens=5,
                        total_tokens=15,
                        prompt="BODY_MUST_NOT_RECORD",
                        cost=0.001,
                    ),
                ),
            )

        model = ExtractionModel(
            self.settings(extraction_model="synthetic-extractor"),
            transport=httpx.MockTransport(handler),
        )
        for step in ("selection", "structure"):
            prompt = "完整的已有" + step + "规则与合成原文"
            result = model.stage(step, records.append).extract(prompt)
            self.assertEqual(result, '{"synthetic":true}')
            self.assertEqual(sent[-1]["messages"], [dict(role="user", content=prompt)])
        self.assertEqual([r["stage"] for r in records], ["selection", "structure"])
        self.assertTrue(all(r["model"] == "synthetic-extractor" for r in records))
        self.assertEqual(records[0]["usage"]["total_tokens"], 15)
        self.assertNotIn("BODY_MUST_NOT_RECORD", str(records))
        self.assertNotIn("完整的已有", str(records))

    def test_json_mode_only_for_hosts_that_document_it(self):
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=dict(choices=[dict(message=dict(content="{}"), finish_reason="stop")]))

        for base, expected in (("https://api.deepseek.com/v1", {"type": "json_object"}),
                               ("https://openrouter.ai/api/v1", {"type": "json_object"}),
                               ("https://upstream.example.test/v1", None)):
            settings = replace(self.settings(extraction_model="synthetic-extractor"), upstream_base_url=base)
            ExtractionModel(settings, transport=httpx.MockTransport(handler)).stage("structure", lambda _: None).extract("合成提示")
            self.assertEqual(sent[-1].get("response_format"), expected)

    def test_truncated_reply_keeps_known_usage_without_response_or_key(self):
        records = []

        def handler(request):
            return httpx.Response(
                200,
                json=dict(
                    choices=[
                        dict(
                            message=dict(content="PRIVATE_BODY"), finish_reason="length"
                        )
                    ],
                    usage=dict(prompt_tokens=12, completion_tokens=60),
                ),
            )

        model = ExtractionModel(
            self.settings(extraction_model="synthetic-extractor"),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(CompletionModelError):
            model.stage("selection", records.append).extract("合成提示")
        self.assertEqual(records[0]["state"], "failed")
        self.assertEqual(records[0]["usage"]["completion_tokens"], 60)
        self.assertNotIn("PRIVATE_BODY", str(records))
        self.assertNotIn("synthetic-key", str(records))

    def test_http_failure_and_malicious_usage_do_not_record_private_text(self):
        records = []

        def handler(request):
            return httpx.Response(503, json=dict(error=dict(message="PRIVATE_ERROR")))

        model = ExtractionModel(
            self.settings(extraction_model="synthetic-extractor"),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(CompletionModelError):
            model.stage("structure", records.append).extract("合成提示")
        self.assertEqual(records[0]["code"], "extraction_structure_http_503")
        self.assertIsNone(records[0]["usage"])
        self.assertNotIn("PRIVATE_ERROR", str(records))
        self.assertEqual(
            safe_usage(
                dict(
                    prompt_tokens=True,
                    total_tokens=-1,
                    cost=float("nan"),
                    completion_tokens_details=dict(reasoning_tokens=2, text="PRIVATE"),
                )
            ),
            dict(completion_tokens_details=dict(reasoning_tokens=2)),
        )
