from dataclasses import replace
import time
import unittest
import json

import httpx

from memory_core.http_models import EmbeddingDimensionError, HTTPRecallModels
from memory_core.http_transport import ModelCallError, call_deadline
from memory_core.model_config import ModelRole, RecallModelsConfig
from memory_core.rerank import order_candidates
from memory_core.rerank_input import ranking_query
from memory_core.testing.provider_server import ProviderServer


class HTTPModelTests(unittest.TestCase):
    def setUp(self):
        self.server = ProviderServer()
        self.addCleanup(self.server.close)
        self.config = RecallModelsConfig(
            embedding=ModelRole("synthetic-embedding"),
            cleaner=ModelRole("synthetic-keyword"),
            reranker=ModelRole("synthetic-voyage", self.server.url, "voyage-only"),
            selector=ModelRole("synthetic-selector"),
        )
        self.models = HTTPRecallModels(
            self.config, 3, main_url=self.server.url, main_key="synthetic-main"
        )

    def test_four_roles_follow_official_wire_formats_and_existing_prompts(self):
        self.models.verify_dimensions()
        self.assertEqual(self.models.embed("合成正文"), [1, 0, 0])
        self.server.replies["synthetic-keyword"] = "旅行 徒步"
        self.assertEqual(
            self.models.clean_query("合成提问", prompt="已有关键词提示词"), "旅行 徒步"
        )
        self.assertEqual(
            self.models.rerank(
                "已有Voyage指令", [7, 2, 5], documents=["甲", "乙", "丙"]
            ),
            [2, 1, 0],
        )
        self.assertEqual(
            self.models.select("合成提问", [7, 2, 5], prompt="已有第二级提示词"),
            "1,2,3,4,5",
        )
        embedding = self.server.requests[1]
        self.assertEqual(
            embedding["body"],
            dict(
                model="synthetic-embedding", input="合成正文", encoding_format="float"
            ),
        )
        voyage = self.server.requests[3]
        self.assertEqual(voyage["authorization"], "Bearer voyage-only")
        self.assertNotIn("truncation", voyage["body"])
        self.assertEqual(voyage["body"]["top_k"], 3)
        self.assertEqual(
            self.server.requests[-1]["body"]["messages"][0]["content"],
            "已有第二级提示词",
        )
        self.assertEqual(self.server.requests[-1]["body"]["max_tokens"], 60)
        self.assertEqual(self.server.requests[-1]["body"]["temperature"], 0)

    def test_document_rerank_formats_send_the_expected_query_and_request_fields(self):
        query = "合成查询"
        candidates = [dict(memory_id=8, content="甲"), dict(memory_id=3, content="乙")]
        rows = [dict(index=1, relevance_score=0.9), dict(index=0, relevance_score=0.1)]
        for env, results_key, expected_query, expected_options in (
            (
                {},
                "data",
                ranking_query(query),
                dict(top_k=2, return_documents=False),
            ),
            ({"MEMORY_DOCUMENT_RERANK_FORMAT": "cohere"}, "results", query, dict(top_n=2)),
        ):
            with self.subTest(results_key=results_key):
                config = RecallModelsConfig.from_env(
                    dict(
                        env,
                        MEMORY_DOCUMENT_RERANK_MODEL="synthetic-reranker",
                        MEMORY_DOCUMENT_RERANK_BASE_URL=self.server.url,
                        MEMORY_DOCUMENT_RERANK_API_KEY="synthetic-rerank-key",
                    )
                )
                self.server.hook = lambda path, body: (200, {results_key: rows})
                models = HTTPRecallModels(config, 3)
                ordered, stats = order_candidates(query, candidates, models, full_list=True)
                self.assertEqual(ordered, candidates[::-1])
                self.assertEqual(stats["ordered_ids"], [3, 8])
                request = self.server.requests[-1]
                self.assertEqual(request["path"], "/v1/rerank")
                self.assertEqual(request["authorization"], "Bearer synthetic-rerank-key")
                self.assertEqual(
                    request["body"],
                    dict(
                        model="synthetic-reranker",
                        query=expected_query,
                        documents=["内容: 甲", "内容: 乙"],
                        **expected_options,
                    ),
                )

    def test_mismatched_document_rerank_response_keeps_coarse_order_and_records_code(self):
        candidates = [dict(memory_id=8, content="甲"), dict(memory_id=3, content="乙")]
        rows = [dict(index=1, relevance_score=0.9), dict(index=0, relevance_score=0.1)]
        for configured_format, wrong_key in (("voyage", "results"), ("cohere", "data")):
            with self.subTest(configured_format=configured_format):
                config = replace(self.config, document_rerank_format=configured_format)
                self.server.hook = lambda path, body: (200, {wrong_key: rows})
                ordered, stats = order_candidates(
                    "合成查询", candidates, HTTPRecallModels(config, 3), full_list=True
                )
                self.assertEqual(ordered, candidates)
                self.assertTrue(stats["skipped"])
                self.assertEqual(stats["error"], "document_rerank_format_mismatch")

    def test_every_embedding_rejects_dimension_drift_and_invalid_vectors(self):
        self.models.verify_dimensions()
        self.server.vector = [1, 0]
        with self.assertRaisesRegex(EmbeddingDimensionError, "2 维.*3 维"):
            self.models.embed("后续写卡")
        for vector in ([0, 0, 0], [True, 0, 0], [float("nan"), 0, 1]):
            self.server.vector = vector
            with self.assertRaises(ModelCallError):
                self.models.embed("合成输入")

    def test_embedding_prefix_dimensions_and_compatible_truncation(self):
        cases = (
            ("google/gemini-embedding-2", "query", None, "task: search result | query: 合成"),
            ("gemini-embedding-2", "document", "标题", "title: 标题 | text: 合成"),
            ("gemini-embedding-2", "document", None, "title: none | text: 合成"),
            ("gemini-embedding-001", "query", None, "合成"),
            ("openai/text-embedding-3-small", "document", "标题", "合成"),
        )
        self.server.vector = [3, 4, 0, 12]
        for name, purpose, title, expected in cases:
            with self.subTest(name=name, purpose=purpose, title=title):
                config = replace(self.config, embedding=ModelRole(name))
                models = HTTPRecallModels(
                    config, 3, main_url=self.server.url, main_key="synthetic"
                )
                self.assertEqual(models.embed_for("合成", purpose=purpose, title=title), [0.6, 0.8, 0])
                self.assertEqual(
                    self.server.requests[-1]["body"],
                    dict(model=name, input=expected, encoding_format="float", dimensions=3),
                )

        self.server.vector = [0, 0, 0, 1]
        with self.assertRaisesRegex(ModelCallError, "invalid_embedding_vector"):
            models.embed_for("合成", purpose="query")
        self.server.vector = [1, 0]
        with self.assertRaises(EmbeddingDimensionError):
            models.embed_for("合成", purpose="query")

    def test_bge_embedding_keeps_original_input_and_exact_dimension(self):
        config = replace(self.config, embedding=ModelRole("BAAI/bge-m3"))
        models = HTTPRecallModels(config, 3, main_url=self.server.url, main_key="synthetic")
        self.assertEqual(models.embed_for("合成", purpose="query"), [1, 0, 0])
        self.assertEqual(
            self.server.requests[-1]["body"],
            dict(model="BAAI/bge-m3", input="合成", encoding_format="float"),
        )
        self.server.vector = [1, 0, 0, 0]
        with self.assertRaises(EmbeddingDimensionError):
            models.embed_for("合成", purpose="document")

    def test_missing_roles_do_not_call_http_or_reuse_chat_key_for_voyage(self):
        missing = HTTPRecallModels(
            RecallModelsConfig(), 3, main_url=self.server.url, main_key="synthetic-main"
        )
        missing.verify_dimensions()
        for call in (
            lambda: missing.embed("合成"),
            lambda: missing.clean_query("合成", prompt="规则"),
            lambda: missing.select("合成", [], prompt="规则"),
            lambda: missing.rerank("合成", [1], documents=["文档"]),
        ):
            with self.assertRaises(ModelCallError):
                call()
        self.assertEqual(self.server.requests, [])

    def test_http_errors_never_return_provider_text_or_configured_keys(self):
        self.server.hook = lambda path, body: (
            503,
            dict(error=dict(message="synthetic-main private response")),
        )
        for call in (
            lambda: self.models.embed("合成"),
            lambda: self.models.clean_query("合成", prompt="规则"),
            lambda: self.models.rerank("合成", [1], documents=["文档"]),
        ):
            with self.assertRaises(ModelCallError) as error:
                call()
            self.assertNotIn("synthetic-main", str(error.exception))
            self.assertNotIn("private response", str(error.exception))

    def test_absolute_call_timeout_uses_remaining_recall_budget(self):
        def slow(path, body):
            time.sleep(0.3)
            return 200, dict(data=[dict(index=0, embedding=[1, 0, 0])])

        self.server.hook = slow
        started = time.monotonic()
        with call_deadline(started + 0.07):
            with self.assertRaisesRegex(ModelCallError, "timeout"):
                self.models.embed("合成")
            time.sleep(0.08)
            with self.assertRaises(ModelCallError):
                self.models.embed("不能再发请求")
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertLessEqual(len(self.server.requests), 1)

    def test_invalid_or_partial_voyage_results_fail_as_a_whole(self):
        for rows in (
            [dict(index=0, relevance_score=0.1)],
            [dict(index=0, relevance_score=0.1), dict(index=0, relevance_score=0.2)],
            [dict(index=0, relevance_score=0.1), dict(index=9, relevance_score=0.2)],
        ):
            self.server.hook = lambda path, body, rows=rows: (200, dict(data=rows))
            with self.assertRaises(ModelCallError):
                self.models.rerank("合成", [1, 2], documents=["甲", "乙"])

    def test_endpoint_pairing_models_and_timeout_are_explicit(self):
        values = RecallModelsConfig.from_env(
            {
                "MEMORY_EMBEDDING_MODEL": "named",
                "MEMORY_RECALL_MODEL_TIMEOUT_SECONDS": "2.5",
            }
        )
        self.assertEqual(values.embedding.model, "named")
        self.assertEqual(values.timeout_seconds, 2.5)
        for value in (
            "https://user:secret@example.test/v1",
            "https://example.test/v1?key=secret",
        ):
            with self.assertRaises(ValueError):
                ModelRole("model", value, "key")
        with self.assertRaises(ValueError):
            ModelRole("model", "https://example.test", None)
        with self.assertRaises(ValueError):
            replace(values, timeout_seconds=float("nan"))
        self.assertNotIn(
            "a-secret", repr(ModelRole("model", "https://example.test", "a-secret"))
        )

    def test_short_chat_roles_disable_reasoning_with_only_known_provider_fields(self):
        for host, expected in (
            ("openrouter.ai", {"reasoning": {"effort": "none"}}),
            ("api.deepseek.com", {"thinking": {"type": "disabled"}}),
            ("api.siliconflow.cn", {"enable_thinking": False}),
            ("api.siliconflow.com", {"enable_thinking": False}),
            ("compatible.example.test", {}),
        ):
            requests = []

            def reply(request):
                requests.append(json.loads(request.content))
                return httpx.Response(
                    200,
                    json=dict(
                        choices=[
                            dict(
                                message=dict(content="1,2,3,4,5"),
                                finish_reason="stop",
                            )
                        ]
                    ),
                )

            with self.subTest(host=host):
                role = ModelRole(
                    "synthetic-short-output", "https://" + host + "/v1", "synthetic-key"
                )
                models = HTTPRecallModels(
                    RecallModelsConfig(cleaner=role, selector=role),
                    3,
                    transport=httpx.MockTransport(reply),
                )
                models.clean_query("合成查询", prompt="已有清洗规则")
                models.select("合成查询", [1, 2, 3, 4, 5], prompt="已有精排规则")
                self.assertEqual(len(requests), 2)
                for body in requests:
                    self.assertEqual(
                        {
                            k: body[k]
                            for k in ("reasoning", "thinking", "enable_thinking")
                            if k in body
                        },
                        expected,
                    )
                    self.assertEqual(body["max_tokens"], 60)
                    self.assertEqual(body["temperature"], 0)
