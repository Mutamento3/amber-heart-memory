from dataclasses import replace
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.models.http_models import EmbeddingDimensionError
from memory_core.storage.install import install
from memory_core.models.model_config import ModelRole, RecallModelsConfig
from memory_core.storage.space import SpaceConfiguration, Participant
from memory_core.storage.postgres import KeywordToken
from memory_core.recall.search_store import SearchStore
from memory_core.testing.provider_server import ProviderServer
from memory_core.tests.support import DatabaseTestCase
from memory_web.admin.access_keys import AccessKeys
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.data import draft

ORIGIN = "https://memory.example.test"


class RecallProviderTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner, Settings(3))
        install_web(self.owner)
        SpaceConfiguration(self.owner).configure(
            "one", Participant("小岚"), Participant("助手")
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password = Path(temporary.name) / "password.hash"
        write_password(password, "synthetic password")
        self.server = ProviderServer()
        self.addCleanup(self.server.close)
        config = RecallModelsConfig(
            embedding=ModelRole("synthetic-embedding"),
            cleaner=ModelRole("synthetic-keyword"),
            reranker=ModelRole(
                "synthetic-voyage", self.server.url, "synthetic-voyage-key"
            ),
            selector=ModelRole("synthetic-final"),
        )
        self.settings = WebSettings(
            self.app,
            self.owner,
            ORIGIN,
            password,
            access_key_master=Fernet.generate_key().decode(),
            upstream_base_url=self.server.url,
            upstream_api_key="synthetic-main-key",
            recall_models=config,
        )

    def tearDown(self):
        pass

    def test_default_factory_fails_before_serving_on_dimension_mismatch(self):
        self.server.vector = [1, 0]
        with self.assertRaisesRegex(EmbeddingDimensionError, "2 维.*3 维"):
            with TestClient(create_app(self.settings), base_url=ORIGIN):
                pass
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) AS n FROM memory_core.memory_items"
                ).fetchone()["n"],
                0,
            )

    def test_approval_uses_the_same_configured_embedding_provider(self):
        card, _ = draft(self.app, "one")
        with TestClient(create_app(self.settings), base_url=ORIGIN) as http:
            csrf = http.post(
                "/api/login",
                headers={"Origin": ORIGIN},
                json={"password": "synthetic password"},
            ).json()["csrf"]
            response = http.post(
                "/api/spaces/one/drafts/" + str(card["id"]) + "/approve",
                headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
                json=dict(
                    expected_version=card["version"],
                    expected_policy_revision=1,
                    request_key="synthetic-approval",
                ),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "approved")
        calls = [r for r in self.server.requests if r["path"].endswith("/embeddings")]
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all(r["body"]["model"] == "synthetic-embedding" for r in calls))
        with connect(self.app) as c:
            self.assertEqual(
                c.execute(
                    "SELECT public.vector_dims(embedding) AS n FROM memory_core.memory_items"
                ).fetchone()["n"],
                3,
            )

    def test_unconfigured_roles_make_no_network_calls_on_startup(self):
        settings = replace(self.settings, recall_models=RecallModelsConfig())
        with TestClient(create_app(settings), base_url=ORIGIN):
            pass
        self.assertEqual(self.server.requests, [])

    def test_default_proxy_degrades_failed_http_roles_and_keeps_chat_working(self):
        store = SearchStore(self.app, "one")
        for i in range(7):
            store.write_card(
                "合成植物卡片正文" + str(i), [1, 0, 0], [KeywordToken("植物")]
            )
        token = AccessKeys(
            self.app, self.owner, self.settings.access_key_master
        ).rotate("one")["key"]
        with TestClient(create_app(self.settings), base_url=ORIGIN) as http:

            def responses(path, body):
                if path.endswith("/embeddings"):
                    return 200, dict(data=[dict(index=0, embedding=[1, 0, 0])])
                if path.endswith("/rerank") or body["model"] != "synthetic-main":
                    return 503, dict(error=dict(message="synthetic provider error"))
                return 200, dict(
                    choices=[
                        dict(
                            message=dict(role="assistant", content="合成回答"),
                            finish_reason="stop",
                        )
                    ]
                )

            self.server.hook = responses
            response = http.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + token},
                json=dict(
                    model="synthetic-main",
                    messages=[
                        dict(
                            role="user",
                            content="请和我说一说合成植物卡片中那些往事与安排。",
                        )
                    ],
                ),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["choices"][0]["message"]["content"], "合成回答"
            )
        with connect(self.app) as c:
            snapshot = c.execute(
                "SELECT recall_snapshot FROM memory_core.conversation_turns"
            ).fetchone()["recall_snapshot"]
            self.assertEqual(snapshot["status"], "degraded")
            self.assertEqual(len(snapshot["cards"]), 5)
            self.assertEqual(snapshot["selection"]["fallback"], "coarse_top5")

    def test_model_config_from_environment_has_no_hidden_model_defaults(self):
        values = RecallModelsConfig.from_env({})
        self.assertIsNone(values.embedding.model)
        self.assertIsNone(values.cleaner.model)
        self.assertIsNone(values.reranker.model)
        self.assertIsNone(values.selector.model)
