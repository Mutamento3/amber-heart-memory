from contextlib import redirect_stdout
import io
import json
import unittest

import httpx
from psycopg.conninfo import conninfo_to_dict

from memory_web.deploy.runtime import deploy_environment, verify_models


class DeployRuntimeTests(unittest.TestCase):
    def test_passwords_are_quoted_and_destination_cannot_be_overridden(self):
        password = "synthetic quote ' and space \\ dbname=other"
        values = deploy_environment(
            dict(
                MEMORY_DB_OWNER_PASSWORD=password,
                MEMORY_DB_APP_PASSWORD=password,
                MEMORY_DSN="host=other dbname=private",
                MEMORY_CONFIG_DSN="host=other dbname=private",
            )
        )
        for variable, role in (
            ("MEMORY_CONFIG_DSN", "memory_owner"),
            ("MEMORY_DSN", "memory_app"),
        ):
            parsed = conninfo_to_dict(values[variable])
            self.assertEqual(parsed["host"], "db")
            self.assertEqual(parsed["dbname"], "amber_heart")
            self.assertEqual(parsed["user"], role)
            self.assertEqual(parsed["password"], password)
        self.assertEqual(
            values["MEMORY_WEB_PASSWORD_FILE"], "/var/lib/memory-admin/password.hash"
        )

    def test_missing_database_credentials_fail_without_echoing_values(self):
        with self.assertRaisesRegex(ValueError, "数据库密码未配置"):
            deploy_environment(dict(MEMORY_DB_APP_PASSWORD="synthetic-secret"))

    def test_verify_models_checks_each_role_once_and_allows_skipping_rerank(self):
        calls = []

        def respond(request):
            body = json.loads(request.content)
            calls.append((request.url.host, request.url.path, body["model"]))
            if request.url.path.endswith("/embeddings"):
                self.assertEqual(body["dimensions"], 3)
                self.assertTrue(body["input"].startswith("task: search result | query: "))
                return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0, 0]}]})
            if request.url.path.endswith("/rerank"):
                self.assertEqual(body["top_k"], 2)
                return httpx.Response(200, json={"data": [
                    {"index": 0, "relevance_score": 1},
                    {"index": 1, "relevance_score": 0},
                ]})
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "散步"}, "finish_reason": "stop"},
            ]})

        env = {
            "MEMORY_EMBEDDING_MODEL": "google/gemini-embedding-2",
            "MEMORY_EMBEDDING_BASE_URL": "https://embeddings.example.test/v1",
            "MEMORY_EMBEDDING_API_KEY": "embedding-key",
            "MEMORY_UPSTREAM_BASE_URL": "https://chat.example.test/v1",
            "MEMORY_UPSTREAM_API_KEY": "chat-key",
            "MEMORY_KEYWORD_MODEL": "light",
        }
        rerank = {
            "MEMORY_DOCUMENT_RERANK_MODEL": "rerank-3",
            "MEMORY_DOCUMENT_RERANK_BASE_URL": "https://rerank.example.test/v1",
            "MEMORY_DOCUMENT_RERANK_API_KEY": "rerank-key",
        }
        with redirect_stdout(io.StringIO()):
            verify_models(env | rerank, 3, transport=httpx.MockTransport(respond))
        self.assertEqual(calls, [
            ("embeddings.example.test", "/v1/embeddings", "google/gemini-embedding-2"),
            ("rerank.example.test", "/v1/rerank", "rerank-3"),
            ("chat.example.test", "/v1/chat/completions", "light"),
        ])
        del calls[:]
        with redirect_stdout(io.StringIO()):
            verify_models(env, 3, transport=httpx.MockTransport(respond))
        self.assertEqual([host for host, _, _ in calls], [
            "embeddings.example.test", "chat.example.test",
        ])

    def test_verify_models_explains_the_failed_role_without_provider_details(self):
        env = {
            "MEMORY_DOCUMENT_RERANK_MODEL": "rerank-3",
            "MEMORY_DOCUMENT_RERANK_BASE_URL": "https://rerank.example.test/v1",
            "MEMORY_DOCUMENT_RERANK_API_KEY": "synthetic-secret",
        }
        transport = httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "synthetic-secret"})
        )
        with self.assertRaisesRegex(ValueError, "第一级精排验证失败.*API key") as error:
            verify_models(env, 3, transport=transport)
        self.assertNotIn("synthetic-secret", str(error.exception))
