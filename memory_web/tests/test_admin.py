from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import json
import re
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.drafts import DraftStore
from memory_core.install import install, InstallationMismatch
from memory_core.keyword_index import build_rows
from memory_core.postgres import PostgresStore
from memory_core.review import ReviewStore
from memory_core.schema import LATEST_VERSION
from memory_core.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase
from memory_web.app import create_app
from memory_web.auth import Sessions, RateLimited, AuthenticationError, BEIJING
from memory_web.config import WebSettings
from memory_web.install import install_web
from memory_web.password import write_password
from memory_web.testing.data import draft

PASSWORD = "synthetic acceptance password"
ORIGIN = "https://memory.example.test"


class AdminTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.password = self.directory / "password.hash"
        write_password(self.password, PASSWORD)
        install(self.owner, Settings(3))
        install_web(self.owner)
        self.settings = WebSettings(self.app, self.owner, ORIGIN, self.password)
        self.config = SpaceConfiguration(self.owner)
        self.http = TestClient(
            create_app(self.settings), base_url=ORIGIN, follow_redirects=False
        )
        self.addCleanup(self.http.close)
        self.csrf = None

    def tearDown(self):
        pass

    def login(self, http=None, remember=False):
        http = http or self.http
        response = http.post(
            "/api/login",
            headers={"Origin": ORIGIN},
            json={"password": PASSWORD, "remember": remember},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf"]
        return response

    def write(self, path, body, method="PUT", http=None):
        return (http or self.http).request(
            method,
            path,
            json=body,
            headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
        )

    def space(self, name="sample"):
        return self.config.configure(
            name,
            Participant("小禾", ["禾禾"]),
            Participant("林舟", ["阿林"]),
            ignored_terms=["亲爱的"],
        )

    def ready(self, key="one", **values):
        return draft(self.app, "sample", key, **values)

    def fixed_client(self, fixtures):
        file = self.directory / "fixed.json"
        file.write_text(json.dumps(fixtures))
        settings = WebSettings(
            self.app, self.owner, ORIGIN, self.password, fixed_models_file=file
        )
        http = TestClient(create_app(settings), base_url=ORIGIN, follow_redirects=False)
        self.addCleanup(http.close)
        self.login(http)
        return http

    def approve(self, draft, http=None, key="review:one"):
        return self.write(
            "/api/spaces/sample/drafts/" + str(draft["id"]) + "/approve",
            dict(
                expected_version=draft["version"],
                expected_policy_revision=draft["policy_revision"],
                request_key=key,
            ),
            method="POST",
            http=http,
        )

    def test_sessions_shared_restart_logout_cookie_and_password_reset(self):
        self.assertEqual(self.http.get("/drafts").status_code, 303)
        self.assertEqual(self.http.get("/api/spaces").status_code, 401)
        response = self.login(remember=True)
        cookie = response.headers["set-cookie"]
        for flag in (
            "HttpOnly",
            "Secure",
            "SameSite=strict",
            "Max-Age=2592000",
            "Path=/",
        ):
            self.assertIn(flag, cookie)
        token = self.http.cookies.get(self.settings.cookie)
        second = TestClient(create_app(self.settings), base_url=ORIGIN)
        second.cookies.update(self.http.cookies)
        self.addCleanup(second.close)
        for page in (
            "overview",
            "review",
            "memories",
            "settings",
            "drafts",
            "cards",
            "participants",
            "topics",
        ):
            self.assertEqual(second.get("/" + page).status_code, 200)
        self.assertEqual(second.get("/api/session").json()["csrf"], self.csrf)
        self.assertEqual(self.write("/api/logout", {}, "POST", second).status_code, 200)
        self.assertIsNone(Sessions(self.app, self.password).lookup(token))
        response = self.login()
        self.assertNotIn("Max-Age", response.headers["set-cookie"])
        write_password(self.password, "replacement synthetic password")
        self.assertEqual(self.http.get("/api/session").status_code, 401)
        with connect(self.app) as connection:
            row = connection.execute(
                "SELECT * FROM memory_web.admin_sessions LIMIT 1"
            ).fetchone()
            self.assertNotIn(token, json.dumps(row))
            self.assertNotIn(PASSWORD, json.dumps(row))
            self.assertTrue(row["expires_at"].endswith("+08:00"))

    def test_expiry_global_limit_survives_workers_and_concurrency(self):
        sessions = Sessions(self.app, self.password)
        now = datetime(2026, 10, 30, 12, tzinfo=BEIJING)
        token, _, _ = sessions.login(PASSWORD, False, now=now)
        self.assertIsNone(sessions.lookup(token, now=now + timedelta(hours=12)))

        def bad(_):
            try:
                Sessions(self.app, self.password).login("wrong", False, now=now)
            except RateLimited:
                return "limited"
            except AuthenticationError:
                return "wrong"

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(bad, range(12)))
        self.assertEqual(results.count("wrong"), 9)
        self.assertEqual(results.count("limited"), 3)
        sessions.login(PASSWORD, False, now=now + timedelta(minutes=6))

    def test_csrf_origin_host_and_machine_credentials(self):
        self.assertEqual(
            self.http.post("/api/login", json={"password": PASSWORD}).status_code, 403
        )
        self.assertEqual(
            self.http.get(
                "/api/session", headers={"Authorization": "Bearer machine-credential"}
            ).status_code,
            401,
        )
        self.login()
        self.assertEqual(
            self.http.post(
                "/api/logout", headers={"Origin": ORIGIN}, json={}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.http.post(
                "/api/logout",
                headers={"Origin": "https://other.test", "X-CSRF-Token": self.csrf},
                json={},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.http.post(
                "/api/logout",
                headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
                content="{}",
            ).status_code,
            415,
        )
        self.assertEqual(
            self.http.get("/api/spaces", headers={"Host": "evil.example"}).status_code,
            400,
        )
        self.assertEqual(self.http.get("/drafts").headers["cache-control"], "no-store")
        self.assertIn(
            "script-src 'self'",
            self.http.get("/drafts").headers["content-security-policy"],
        )

    def test_diary_excerpt_drops_title_and_folds_lines(self):
        from memory_web.catalog import diary_excerpt
        self.assertEqual(diary_excerpt("# 林舟的日记\n今天\n\n  去散步了。"), "今天 去散步了。")
        self.assertEqual(diary_excerpt("顾言的日记\n周六。"), "周六。")
        self.assertEqual(diary_excerpt("今天的日记很短。"), "今天的日记很短。")
        self.assertEqual(diary_excerpt("网页编辑的日记"), "网页编辑的日记")
        self.assertEqual(diary_excerpt("字" * 130), "字" * 120 + "…")

    def test_static_files_are_versioned_compressed_and_cached(self):
        page = self.http.get("/login", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(page.headers["cache-control"], "no-store")
        versions = set(re.findall(r'\?v=([0-9a-f]{12})"', page.text))
        self.assertEqual(len(versions), 1, page.text)
        version = versions.pop()
        for name in ("app.js", "style.css", "manifest.webmanifest", "icons/apple-touch-icon.png"):
            self.assertIn(f"./static/{name}?v={version}", page.text)
        script = self.http.get(f"/static/app.js?v={version}", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(script.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(script.headers["content-encoding"], "gzip")
        self.assertIn(f'./memos.js?v={version}"', script.text)
        self.assertEqual(self.http.get("/static/app.js?v=old").headers["cache-control"], "no-cache")
        again = self.http.get(f"/static/app.js?v={version}", headers={"Accept-Encoding": "gzip", "If-None-Match": script.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        manifest = self.http.get(f"/static/manifest.webmanifest?v={version}")
        self.assertEqual(manifest.headers["content-type"], "application/manifest+json")
        self.assertEqual(manifest.json()["start_url"], "../overview")
        self.assertTrue(all(icon["src"].endswith("?v=" + version) for icon in manifest.json()["icons"]))
        icon = self.http.get(f"/static/icons/icon-512.png?v={version}", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(icon.headers["content-type"], "image/png")
        self.assertNotIn("content-encoding", icon.headers)
        self.assertEqual(self.http.get("/static/%2e%2e/app.py").status_code, 404)

    def test_default_factory_empty_and_missing_configuration_never_loads_private_modules(
        self,
    ):
        values = {
            "MEMORY_DSN": self.app,
            "MEMORY_CONFIG_DSN": self.owner,
            "MEMORY_WEB_ORIGIN": ORIGIN,
            "MEMORY_WEB_PASSWORD_FILE": str(self.password),
        }
        with patch.dict(os.environ, values, clear=True):
            with TestClient(create_app(), base_url=ORIGIN) as client:
                self.login(client)
                self.assertEqual(client.get("/api/spaces").json(), [])
                self.assertFalse(client.get("/api/session").json()["fixed_models"])
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            ValueError, "MEMORY_DSN"
        ):
            create_app()
        with self.assertRaises(ValueError):
            WebSettings(
                self.app, self.owner, "http://public.example", self.password, True
            )
        with self.assertRaises(FileNotFoundError):
            create_app(
                WebSettings(
                    self.app, self.owner, ORIGIN, self.password.parent / "absent"
                )
            )

    def test_default_install_password_and_server_commands(self):
        installed = subprocess.run(
            [sys.executable, "-m", "memory_web.install"],
            env={"MEMORY_INSTALL_DSN": self.owner},
            text=True,
            capture_output=True,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn("一致", installed.stdout)
        before = Sessions(self.app, self.password).login(PASSWORD, False)[0]
        replacement = "new synthetic administrator phrase"
        changed = subprocess.run(
            [sys.executable, "-m", "memory_web.password", "--file", str(self.password)],
            input=replacement + "\n" + replacement + "\n",
            env={},
            text=True,
            capture_output=True,
        )
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertNotIn(replacement, changed.stdout + changed.stderr)
        self.assertIsNone(Sessions(self.app, self.password).lookup(before))
        self.assertIsNotNone(
            Sessions(self.app, self.password).login(replacement, False)
        )
        environment = {
            "MEMORY_DSN": self.app,
            "MEMORY_CONFIG_DSN": self.owner,
            "MEMORY_WEB_ORIGIN": "http://127.0.0.1:8080",
            "MEMORY_WEB_LOCAL_HTTP": "1",
            "MEMORY_WEB_PASSWORD_FILE": str(self.password),
        }
        server = subprocess.Popen(
            [sys.executable, "-m", "memory_web"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(80):
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:8080/login", timeout=1
                    ) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn("memory_web", server.args[-1])
                        break
                except OSError:
                    if server.poll() is not None:
                        self.fail("default server exited before serving login")
                    time.sleep(0.1)
            else:
                self.fail("default server did not start")
        finally:
            server.terminate()
            server.wait(timeout=5)

    def test_web_install_idempotence_permissions_and_drift(self):
        self.assertFalse(install_web(self.owner))
        self.assertFalse(install(self.owner, Settings(3)))
        with self.assertRaises(ValueError):
            install_web(self.app)
        with connect(self.app) as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("CREATE TABLE memory_web.unauthorized(id int)")
        with connect(self.owner) as connection:
            connection.execute(
                "ALTER TABLE memory_web.admin_sessions ADD COLUMN drift text"
            )
        with self.assertRaises(InstallationMismatch):
            install_web(self.owner)

    def test_first_space_participants_alias_conflicts_and_fixed_templates(self):
        self.login()
        body = dict(
            expected_revision=0,
            user={"name": "小禾", "aliases": ["禾禾"]},
            assistant={"name": "林舟", "aliases": ["阿林"]},
        )
        response = self.write("/api/spaces/sample/participants", body)
        self.assertEqual(response.status_code, 200, response.text)
        config = response.json()
        self.assertEqual(config["topic_template"], "lover")
        self.assertTrue(all(not t["summary_eligible"] for t in config["topics"]))
        body["expected_revision"] = config["revision"]
        body["assistant"]["aliases"] = ["禾禾"]
        self.assertEqual(
            self.write("/api/spaces/sample/participants", body).status_code, 400
        )
        body["assistant"]["aliases"] = ["亲爱的"]
        self.assertEqual(
            self.write("/api/spaces/sample/participants", body).status_code, 400
        )
        body["assistant"]["aliases"] = ["阿林"]
        body["assistant"]["name"] = "林溪"
        response = self.write("/api/spaces/sample/participants", body)
        self.assertEqual(response.status_code, 200, response.text)
        renamed = response.json()
        self.assertEqual(
            renamed["participants"]["assistant"]["id"],
            config["participants"]["assistant"]["id"],
        )
        self.assertEqual(renamed["topics"], config["topics"])
        self.assertEqual(self.http.get("/api/spaces").json()[0]["name"], "林溪")
        topic = renamed["topics"][0]
        endpoint = "/api/spaces/sample/topics/" + str(topic["id"])
        self.assertEqual(self.write(endpoint, dict(expected_revision=2, name="新主题")).status_code, 404)
        self.assertEqual(self.http.get(endpoint + "/impact").status_code, 404)
        body["expected_revision"] = 1
        self.assertEqual(self.write("/api/spaces/sample/participants", body).status_code, 409)
        body["expected_revision"] = 2
        for template in ("lover", "general"):
            self.assertEqual(self.write("/api/spaces/sample/participants", dict(body, topic_template=template)).status_code, 400)
        self.assertEqual(self.write("/api/spaces/sample/participants", dict(body, ignored_terms=[])).status_code, 422)
        with connect(self.app) as connection:
            ignored = connection.execute("SELECT ignored_terms FROM memory_core.space_policies WHERE bot_id='sample'").fetchone()["ignored_terms"]
        self.assertEqual(ignored, ["宝宝", "宝贝", "亲爱的", "老公", "老婆", "Daddy"])
        general = self.write("/api/spaces/general/participants", dict(body, expected_revision=0, topic_template="general"))
        self.assertEqual(general.status_code, 400, general.text)
        self.config.configure(
            "general",
            Participant("合成读者"),
            Participant("合成助手"),
            topic_template="general",
        )
        with connect(self.app) as connection:
            self.assertEqual(connection.execute("SELECT ignored_terms FROM memory_core.space_policies WHERE bot_id='general'").fetchone()["ignored_terms"], [])

    def test_draft_save_conflict_evidence_and_normal_mode_no_embedder(self):
        self.space()
        actual, _ = self.ready()
        self.login()
        path = "/api/spaces/sample/drafts/" + str(actual["id"])
        payload = deepcopy(actual["payload"])
        payload["title"] = "修改后的标题"
        body = dict(
            expected_version=actual["version"],
            expected_policy_revision=actual["policy_revision"],
            payload=payload,
        )
        response = self.write(path, body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["payload"]["title"], "修改后的标题")
        self.assertEqual(self.write(path, body).status_code, 409)
        actual = response.json()
        body["expected_version"] = actual["version"]
        body["payload"]["evidence"][0]["quote"] = "原文不存在的证据"
        self.assertEqual(self.write(path, body).status_code, 400)
        response = self.approve(actual)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("嵌入", response.text)
        self.assertEqual(self.http.get(path).json()["draft"]["status"], "pending")
        body["payload"] = actual["payload"]
        body["payload"]["summary"] = "失败后继续修改"
        self.assertEqual(self.write(path, body).status_code, 200)

    def test_approval_idempotence_concurrency_and_sources(self):
        self.space()
        actual, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.approve(actual, client), range(2)))
        self.assertTrue(
            all(r.status_code == 200 for r in responses), [r.text for r in responses]
        )
        mid = responses[0].json()["memory_item_id"]
        self.assertEqual(responses[1].json()["memory_item_id"], mid)
        self.assertEqual(self.approve(actual, client).json()["memory_item_id"], mid)
        self.assertEqual(
            client.get("/api/spaces/sample/drafts?status=processed").json()["total"], 1
        )
        self.assertEqual(
            client.get("/api/spaces/sample/drafts?status=pending").json()["total"], 0
        )
        card = client.get("/api/spaces/sample/cards/" + str(mid)).json()
        self.assertEqual(card["source"]["source_status"], "available")
        self.assertEqual(len(card["entities"]), 2)
        self.assertTrue(card["topics"])
        source = DraftStore(self.app, "sample").source(actual["id"])
        DraftStore(self.app, "sample").delete_material(source["id"])
        self.assertEqual(
            client.get("/api/spaces/sample/cards/" + str(mid)).json()["source"][
                "source_status"
            ],
            "deleted",
        )
        self.assertEqual(
            client.get("/api/spaces/sample/cards?q=植物").json()["total"], 1
        )

    def test_identical_draft_can_be_approved_and_deleted_from_the_web(self):
        self.space()
        first, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        first_result = self.approve(first, client).json()
        second, _ = self.ready("same-content")
        review_url = f"/api/spaces/sample/drafts/{second['id']}"

        def check_draft(status):
            response = client.get(review_url)
            self.assertEqual(response.status_code, 200, response.text)
            draft = response.json()["draft"]
            self.assertEqual(draft["status"], status)
            self.assertIsNone(draft["dedup_state"])
            self.assertIsNone(draft["duplicate_memory_id"])

        check_draft("pending")
        response = self.approve(second, client, key="review:same-content")
        self.assertEqual(response.status_code, 200, response.text)
        added = response.json()
        self.assertEqual(added["status"], "approved")
        self.assertNotEqual(added["memory_item_id"], first_result["memory_item_id"])
        check_draft("approved")
        processed = client.get("/api/spaces/sample/drafts?status=processed").json()
        self.assertEqual(processed["total"], 2)
        self.assertTrue(all(item["duplicate_memory_id"] is None for item in processed["items"]))
        self.assertEqual(client.get("/api/spaces/sample/drafts?status=rejected").json()["total"], 0)

        delete_url = f"/api/spaces/sample/cards/{added['memory_item_id']}"
        self.assertEqual(self.write(delete_url, {}, method="DELETE", http=client).status_code, 200)
        remaining = client.get(f"/api/spaces/sample/cards/{first_result['memory_item_id']}")
        self.assertEqual(remaining.status_code, 200, remaining.text)

    def test_renames_reindex_existing_cards_atomically_without_rewriting_them(self):
        self.space()
        actual, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        approved = self.approve(actual, client).json()
        mid = approved["memory_item_id"]
        before = client.get("/api/spaces/sample/cards/" + str(mid)).json()
        self.config.configure(
            "sample",
            Participant("紫菀", ["橡果"]),
            Participant("林舟", ["阿林"]),
            ignored_terms=["亲爱的"],
            expected_revision=1,
        )
        self.assertEqual(
            client.get("/api/spaces/sample/cards?q=紫菀").json()["total"], 1
        )
        self.assertEqual(
            client.get("/api/spaces/sample/cards?q=橡果").json()["total"], 1
        )
        after = client.get("/api/spaces/sample/cards/" + str(mid)).json()
        self.assertEqual(after["content"], before["content"])
        self.assertEqual(after["primary_topic_id"], before["primary_topic_id"])
        with patch("memory_core.keyword_index.build_rows", side_effect=RuntimeError("fixed reindex failure")):
            with self.assertRaises(RuntimeError):
                self.config.configure("sample", Participant("不会提交"), Participant("林舟"), expected_revision=2)
        latest = client.get("/api/spaces/sample/configuration").json()
        self.assertEqual(latest["revision"], 2)
        self.assertEqual(latest["participants"]["user"]["name"], "紫菀")
        self.assertEqual(client.get("/api/spaces/sample/cards?q=橡果").json()["total"], 1)

    def test_configuration_conflict_source_deleted_incomplete_and_cross_space(self):
        self.space()
        self.space("other")
        actual, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        self.config.configure("sample", Participant("小禾", ["新别名"]), Participant("林舟", ["阿林"]), expected_revision=1)
        self.assertEqual(self.approve(actual, client).status_code, 409)
        body = dict(
            expected_version=actual["version"],
            expected_policy_revision=2,
            payload=actual["payload"],
        )
        saved = self.write(
            "/api/spaces/sample/drafts/" + str(actual["id"]), body, http=client
        ).json()
        self.assertEqual(saved["policy_revision"], 2)
        self.assertEqual(
            client.get("/api/spaces/other/drafts/" + str(actual["id"])).status_code, 400
        )
        self.assertEqual(client.get("/api/spaces/missing/drafts").status_code, 404)
        source = DraftStore(self.app, "sample").source(actual["id"])
        DraftStore(self.app, "sample").delete_material(source["id"])
        self.assertIn("原文已删除", self.approve(saved, client).text)
        self.assertEqual(
            self.write(
                "/api/spaces/sample/drafts/" + str(actual["id"]) + "/reject",
                {"expected_version": saved["version"]},
                "POST",
                client,
            ).status_code,
            200,
        )
        second, _ = self.ready("incomplete")
        with connect(self.app) as connection:
            connection.execute(
                "UPDATE memory_core.pending_memories SET status='structure_failed',payload=NULL WHERE id=%s",
                (second["id"],),
            )
        self.assertEqual(
            self.approve(second, client, key="incomplete").status_code, 409
        )

    def test_card_metadata_and_archived_browsing_without_any_models(self):
        policy=self.space()
        self.login()
        store=PostgresStore(self.app,"sample")
        mid=store.write_card("合成正文不能修改",[1,0,0],title="原来的合成标题",summary="固定的检索摘要")
        path=f"/api/spaces/sample/cards/{mid}"
        topics=self.http.get("/api/spaces/sample/configuration").json()["topics"]
        body=dict(title="合成银河观测档案",topic_ids=[topics[0]["id"]],is_milestone=True)
        from memory_core.http_models import HTTPRecallModels
        from memory_core.completion_model import CompletionModel
        with patch.object(HTTPRecallModels,"embed",side_effect=AssertionError("must not call embedding")), patch.object(CompletionModel,"complete_sync",side_effect=AssertionError("must not call chat")):
            response=self.write(path,body,method="PATCH")
        self.assertEqual(response.status_code,200,response.text)
        card=self.http.get(path).json()
        self.assertEqual((card["title"],card["content"],card["is_milestone"]),(body["title"],"合成正文不能修改",True))
        self.assertEqual(self.http.get("/api/spaces/sample/cards?q=银河").json()["total"],1)
        for extra in ("content","summary","source_date","experience_mode","entities","expected_version"):
            self.assertEqual(self.write(path,body|{extra:"forbidden"},method="PATCH").status_code,422)
        self.assertEqual(self.http.patch(path,json=body,headers={"Origin":ORIGIN}).status_code,403)
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE id=%s",(mid,))
        self.assertEqual(self.http.get("/api/spaces/sample/cards").json()["total"],0)
        for suffix in ("?archive=archived","?archive=all&q=银河"):
            self.assertEqual(self.http.get("/api/spaces/sample/cards"+suffix).json()["total"],1)
        self.assertEqual(self.write(path,body|{"title":"已归档的合成银河"},method="PATCH").status_code,200)
        self.assertEqual(self.write(path,{},method="DELETE").status_code,200)
        self.assertEqual(self.http.get("/api/spaces/sample/cards?archive=all").json()["total"],0)
        self.assertNotEqual(self.http.get(path).status_code,200)

    def test_card_delete_clears_review_copy_and_retains_daily_quota(self):
        self.space()
        candidate,fixtures=self.ready()
        client=self.fixed_client(fixtures)
        approved=self.approve(candidate,client).json()
        mid=approved["memory_item_id"]
        path=f"/api/spaces/sample/cards/{mid}"
        self.assertEqual(self.write(path,{},method="DELETE",http=client).status_code,200)
        with connect(self.app) as c:
            row=c.execute("SELECT * FROM memory_core.pending_memories WHERE id=%s",(candidate["id"],)).fetchone()
            self.assertEqual(row["status"],"deleted")
            self.assertEqual(row["narrative"],{})
            self.assertIsNone(row["payload"])
            self.assertIsNone(row["memory_item_id"])
            self.assertEqual(c.execute("SELECT count(*) AS n FROM memory_core.pending_memories WHERE business_date=%s",(row["business_date"],)).fetchone()["n"],1)
        self.assertEqual(client.get("/api/spaces/sample/drafts?status=processed").json()["total"],0)
        self.assertNotEqual(client.get(f"/api/spaces/sample/drafts/{candidate['id']}").status_code,200)
        again=self.approve(candidate,client).json()
        self.assertEqual(again["status"],"deleted")
        self.assertIsNone(again["memory_item_id"])
        self.assertEqual(client.get("/api/spaces/sample/cards").json()["total"],0)

    def test_card_mutation_http_database_failure_keeps_original_and_summary_readonly(self):
        self.space()
        self.login()
        store=PostgresStore(self.app,"sample")
        mid=store.write_card("合成保留正文",[1,0,0],title="原卡标题")
        topic=self.http.get("/api/spaces/sample/configuration").json()["topics"][0]["id"]
        from memory_core.cards import CardStore
        def fail(stage,connection):
            connection.execute("SELECT 1/0")
        path=f"/api/spaces/sample/cards/{mid}"
        body=dict(title="尚未保存的标题",topic_ids=[topic],is_milestone=True)
        with patch.object(CardStore,"_checkpoint",side_effect=fail):
            self.assertEqual(self.write(path,body,method="PATCH").status_code,503)
            self.assertEqual(self.write(path,{},method="DELETE").status_code,503)
        self.assertEqual(self.http.get(path).json()["title"],"原卡标题")
        parent=store.write_card("合成主题总结",[0,1,0],item_kind="topic_summary")
        for method,value in (("PATCH",body),("DELETE",{})):
            self.assertEqual(self.write(f"/api/spaces/sample/cards/{parent}",value,method=method).status_code,400)
        self.config.configure("other",Participant("Other User"),Participant("Other Assistant"))
        self.assertEqual(self.write(f"/api/spaces/other/cards/{mid}",{},method="DELETE").status_code,400)

    def test_keyword_pagination_filters_and_read_only_cards(self):
        policy = self.space()
        self.login()
        store = PostgresStore(self.app, "sample")
        for index in range(36):
            mid = store.write_card(
                "合成植物课程 " + str(index),
                [1, 0, 0],
                title="植物 " + str(index),
                summary="植物课程",
                source_date="2026-10-29",
            )
            with connect(self.app) as connection:
                card = connection.execute(
                    "SELECT * FROM memory_core.memory_items WHERE id=%s", (mid,)
                ).fetchone()
                rows = build_rows(
                    card, [], [], timestamp=card["created_at"], ignored_terms=[]
                )
                from psycopg.types.json import Jsonb

                connection.execute(
                    "SELECT memory_core.replace_memory_keyword_tokens(%s,%s,%s)",
                    (mid, "sample", Jsonb(rows)),
                )
        response = self.http.get("/api/spaces/sample/cards?q=植物&page=2&size=20")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 36)
        self.assertEqual(len(response.json()["items"]), 16)
        self.assertEqual(
            self.http.get("/api/spaces/sample/cards?since=2026-10-30").json()["total"],
            0,
        )
        self.assertEqual(
            self.http.get("/api/spaces/sample/cards?topic=999").json()["total"], 0
        )
        self.assertEqual(
            self.http.get(
                "/api/spaces/sample/cards?since=2026-11-01&until=2026-10-01"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.http.get("/api/spaces/sample/cards?page=0").status_code, 422
        )
        self.assertEqual(
            self.write("/api/spaces/sample/cards/1", {"content": "change"}).status_code,
            405,
        )
        self.assertEqual(
            self.http.get("/api/spaces/sample/cards?q=亲爱的").json()["total"], 0
        )

    def test_approved_templates_and_names_reach_the_structure_model(self):
        from memory_core.space import topic_presets

        presets = topic_presets()
        self.assertEqual(len(presets["lover"]), 11)
        self.assertEqual(len(presets["general"]), 8)
        self.assertIn(
            "普通的聊天、撒娇、陪伴不挂这个主题。", presets["lover"][0]["description"]
        )
        self.space()
        item, _ = self.ready()
        prompt = DraftStore(self.app, "sample").structure_prompt(item["id"])
        self.assertIn("小禾和林舟这段感情的转折点", prompt)
        self.assertIn("小禾和林舟之间的亲吻", prompt)
        self.assertIn("现实中的伴侣", prompt)
        self.assertIn("普通的聊天、撒娇、陪伴不挂这个主题。", prompt)
        self.assertNotIn("用户和 AI", prompt)

    def test_maximum_topics_rejected_for_save_and_approval(self):
        self.space()
        item, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        topics = client.get("/api/spaces/sample/configuration").json()["topics"]
        payload = deepcopy(item["payload"])
        payload["topic_ids"] = [t["id"] for t in topics[:5]]
        path = "/api/spaces/sample/drafts/" + str(item["id"])
        response = self.write(
            path,
            dict(
                payload=payload,
                expected_version=item["version"],
                expected_policy_revision=1,
            ),
            http=client,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(client.get(path).json()["draft"]["version"], item["version"])
        from psycopg.types.json import Jsonb

        with connect(self.app) as connection:
            connection.execute(
                "UPDATE memory_core.pending_memories SET payload=%s WHERE id=%s",
                (Jsonb(payload), item["id"]),
            )
        response = self.approve(item, client)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(client.get("/api/spaces/sample/cards").json()["total"], 0)
        self.assertEqual(client.get("/api/contract").json()["max_topics"], 3)

    def test_unfinished_drafts_accept_minimal_review_without_inventing_event_dates(self):
        for status in ("structure_pending", "structure_failed"):
            with self.subTest(status=status):
                self.space(status)
                item, fixtures = draft(self.app, status, status)
                client = self.fixed_client(fixtures)
                with connect(self.app) as connection:
                    connection.execute(
                        "UPDATE memory_core.pending_memories SET payload=NULL,status=%s WHERE id=%s",
                        (status, item["id"]),
                    )
                payload = dict(item["narrative"], experience_mode="external",
                    cognitive_status="stable", decay_layer="tier_4_episode",
                    source_date=None, valence=None, arousal=None, is_milestone=False,
                    entities=[], relations=[], topic_ids=item["payload"]["topic_ids"])
                path = f"/api/spaces/{status}/drafts/{item['id']}"
                response = self.write(path, dict(payload=payload,
                    expected_version=item["version"], expected_policy_revision=1), http=client)
                self.assertEqual(response.status_code, 200, response.text)
                actual = response.json()
                self.assertEqual(actual["payload"], payload)
                self.assertEqual(actual["status"], "pending")
                response = self.write(path + "/approve", dict(expected_version=actual["version"],
                    expected_policy_revision=1, request_key="unfinished:" + status), "POST", client)
                self.assertEqual(response.status_code, 200, response.text)
                card = client.get(f"/api/spaces/{status}/cards/{response.json()['memory_item_id']}").json()
                self.assertIsNone(card["source_date"])
                self.assertEqual(card["cognitive_status"], "stable")
                self.assertEqual(card["decay_layer"], "tier_4_episode")
                self.assertIsNone(card["valence"])
                self.assertIsNone(card["arousal"])
                self.assertEqual(card["entities"], [])
                self.assertEqual(card["source"]["evidence"], item["narrative"]["evidence"])

    def test_invalid_hidden_entity_references_fail_helpfully_without_discarding_them(self):
        from psycopg.types.json import Jsonb

        self.space()
        other = self.space("other")
        item, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        path = f"/api/spaces/sample/drafts/{item['id']}"
        for identifier in (99999999, other["user_entity_id"]):
            with self.subTest(identifier=identifier):
                payload = deepcopy(item["payload"])
                payload["entities"][0]["known_entity_id"] = identifier
                response = self.write(path, dict(payload=payload,
                    expected_version=item["version"], expected_policy_revision=1), http=client)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["code"], "unknown_or_cross_space_entity")
                self.assertIn("当前编辑仍保留", response.json()["detail"])
                self.assertEqual(client.get(path).json()["draft"]["payload"], item["payload"])
                # Simulate a stale reference already present in a stored draft.
                with connect(self.app) as connection:
                    connection.execute("UPDATE memory_core.pending_memories SET payload=%s WHERE id=%s",
                        (Jsonb(payload), item["id"]))
                response = self.approve(item, client)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["code"], "unknown_or_cross_space_entity")
                actual = client.get(path).json()["draft"]
                self.assertEqual(actual["payload"], payload)
                self.assertEqual(actual["version"], item["version"])
                self.assertEqual(actual["status"], "pending")
                with connect(self.app) as connection:
                    connection.execute("UPDATE memory_core.pending_memories SET payload=%s WHERE id=%s",
                        (Jsonb(item["payload"]), item["id"]))
        self.assertEqual(client.get("/api/spaces/sample/cards").json()["total"], 0)

    def test_ambiguous_hidden_entity_is_reported_without_choosing_or_dropping_it(self):
        from memory_core.graph_store import GraphStore

        self.space()
        item, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        graph = GraphStore(self.app, "sample")
        for description in ("合成课程甲", "合成课程乙"):
            graph.create_entity("植物课程", entity_type="EVENT", description=description)
        response = self.approve(item, client)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "ambiguous_entity")
        self.assertIn("暂时分不清指的是谁", response.json()["detail"])
        actual = client.get(f"/api/spaces/sample/drafts/{item['id']}").json()["draft"]
        self.assertEqual(actual["payload"], item["payload"])
        self.assertEqual(actual["status"], "pending")
        self.assertEqual(client.get("/api/spaces/sample/cards").json()["total"], 0)

    def test_only_participant_changes_require_visible_reconfirmation(self):
        self.space()
        pending, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        path = "/api/spaces/sample/drafts/" + str(pending["id"])
        self.config.set_acquaintance_date("sample", expected_revision=1, acquaintance_date="2024-02-29")
        self.assertEqual(client.get(path).json()["configuration_changes"], [])
        self.config.configure("sample", Participant("小禾", ["禾禾"]), Participant("林舟", ["阿林"]),
            expected_revision=2, ignored_terms=["自定义泛称"])
        self.assertEqual(client.get(path).json()["configuration_changes"], [])
        self.config.configure("sample", Participant("紫菀", ["新小名"]), Participant("林舟", ["阿林"]), expected_revision=3)
        detail = client.get(path).json()
        self.assertEqual(detail["configuration_changes"], ["你的名字：小禾 → 紫菀", "你的又名已调整"])
        response = self.write(path, dict(expected_version=pending["version"], expected_policy_revision=4,
            payload=pending["payload"]), http=client)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(client.get(path).json()["configuration_changes"], [])
        self.assertEqual(response.json()["payload"]["experience_mode"], pending["payload"]["experience_mode"])

    def test_legacy_missing_snapshot_is_explained_without_invented_changes(self):
        self.space()
        item, _ = self.ready()
        self.login()
        with connect(self.app) as connection:
            connection.execute(
                "UPDATE memory_core.pending_memories SET policy_snapshot=NULL WHERE id=%s",
                (item["id"],),
            )
        self.config.configure(
            "sample", Participant("新用户"), Participant("林舟"), expected_revision=1
        )
        detail = self.http.get("/api/spaces/sample/drafts/" + str(item["id"])).json()
        self.assertIn("无法逐项还原", detail["configuration_changes"][0])

    def test_overview_counts_days_topics_recent_and_rejected_list(self):
        from datetime import date

        from memory_web.catalog import Catalog

        self.space()
        actual, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        mid = self.approve(actual, client).json()["memory_item_id"]
        second, _ = self.ready("manual-rejection")
        response = self.write(
            f"/api/spaces/sample/drafts/{second['id']}/reject",
            {"expected_version": second["version"]},
            method="POST",
            http=client,
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = client.get("/api/spaces/sample/overview").json()
        self.assertEqual((data["cards"], data["pending"], data["rejected"]), (1, 0, 1))
        self.assertTrue(data["auto_approval"]["enabled"])
        self.assertEqual(data["cards_this_month"], 1)
        self.assertNotIn("recent", data)
        self.assertEqual((data["user"], data["assistant"]), ("小禾", "林舟"))
        card = client.get("/api/spaces/sample/cards/" + str(mid)).json()
        counts = {topic["id"]: topic["cards"] for topic in data["topics"]}
        self.assertTrue(all(counts[topic["id"]] == 1 for topic in card["topics"]))
        self.assertEqual(sum(counts.values()), len(card["topics"]))
        self.assertIsNone(data["days_together"])
        self.assertGreaterEqual(data["days_collected"], 1)
        rejected = client.get("/api/spaces/sample/drafts?status=rejected").json()
        self.assertEqual(rejected["total"], 1)
        self.assertEqual(rejected["items"][0]["status"], "rejected")
        self.assertIsNone(rejected["items"][0]["duplicate_memory_id"])
        self.assertEqual(
            client.get("/api/spaces/sample/drafts?status=pending").json()["total"], 0
        )
        revision = client.get("/api/spaces/sample/configuration").json()["revision"]
        body = dict(expected_revision=revision, acquaintance_date="2026-01-01")
        response = self.write("/api/spaces/sample/configuration", body, "PATCH", client)
        self.assertEqual(response.status_code, 200, response.text)
        catalog = Catalog(self.app)
        # The acquaintance day itself is day 1; future dates have no count yet.
        self.assertEqual(catalog.overview("sample", today=date(2026, 1, 1))["days_together"], 1)
        self.assertEqual(catalog.overview("sample", today=date(2026, 3, 1))["days_together"], 60)
        self.assertIsNone(catalog.overview("sample", today=date(2025, 12, 31))["days_together"])
        self.assertEqual(
            client.get("/api/spaces/other/overview").status_code, 404
        )

    def test_acquaintance_date_configuration_lifecycle_and_older_participant_saves(self):
        self.space()
        self.space("other")
        self.login()
        path = "/api/spaces/sample/configuration"
        self.assertIsNone(self.http.get(path).json()["acquaintance_date"])
        response = self.write(path, dict(expected_revision=1, acquaintance_date="2024-02-29"), "PATCH")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["acquaintance_date"], "2024-02-29")
        self.assertEqual(set(response.json()["review_snapshot"]), {"participants"})
        self.assertIsNone(self.http.get("/api/spaces/other/configuration").json()["acquaintance_date"])
        # Old forms omit the field: they must not erase previously configured metadata.
        body = dict(expected_revision=2, user={"name": "小禾"}, assistant={"name": "林舟"})
        response = self.write("/api/spaces/sample/participants", body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["acquaintance_date"], "2024-02-29")
        response = self.write(path, dict(expected_revision=2, acquaintance_date=None), "PATCH")
        self.assertEqual(response.status_code, 409, response.text)
        body.update(expected_revision=3, acquaintance_date=None)
        response = self.write("/api/spaces/sample/participants", body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["acquaintance_date"])
        body.update(expected_revision=0, acquaintance_date="2020-01-01")
        response = self.write("/api/spaces/created/participants", body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("一套记忆库只服务一个", response.json()["detail"])
        response = self.write(path, dict(expected_revision=4, acquaintance_date="2025-01-01"), "PATCH")
        self.assertEqual(response.status_code, 200, response.text)
        response = self.write(path, dict(expected_revision=5, acquaintance_date=None), "PATCH")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.http.get(path).json()["acquaintance_date"])

    def test_acquaintance_date_validates_auth_types_calendar_and_revision(self):
        self.space()
        path = "/api/spaces/sample/configuration"
        body = dict(expected_revision=1, acquaintance_date="2024-02-29")
        self.assertEqual(self.http.patch(path, json=body, headers={"Origin": ORIGIN}).status_code, 401)
        self.login()
        self.assertEqual(self.http.patch(path, json=body, headers={"Origin": ORIGIN}).status_code, 403)
        for value in (True, 20240229, {}, []):
            with self.subTest(value=value):
                body["acquaintance_date"] = value
                self.assertEqual(self.write(path, body, "PATCH").status_code, 422)
        for value in ("", "2023-02-29", "2024-02-30", "2024-2-29", "0000-01-01", "2024-02-29T00:00:00+08:00"):
            with self.subTest(value=value):
                body["acquaintance_date"] = value
                response = self.write(path, body, "PATCH")
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("相识日", response.json()["detail"])
        for invalid in (dict(expected_revision=1), dict(acquaintance_date=None),
            dict(expected_revision=0, acquaintance_date=None),
            dict(expected_revision=True, acquaintance_date=None),
            dict(expected_revision=1, acquaintance_date=None, unknown="value")):
            self.assertEqual(self.write(path, invalid, "PATCH").status_code, 422)
        self.assertEqual(self.write("/api/spaces/missing/configuration",
            dict(expected_revision=1, acquaintance_date=None), "PATCH").status_code, 404)
        current = self.http.get(path).json()
        self.assertEqual(current["revision"], 1)
        self.assertIsNone(current["acquaintance_date"])

    def test_date_changes_keep_revision_guards_without_visible_reconfirmation(self):
        self.space()
        item, fixtures = self.ready()
        client = self.fixed_client(fixtures)
        path = "/api/spaces/sample/configuration"
        response = self.write(path, dict(expected_revision=1, acquaintance_date="2024-02-29"), "PATCH", client)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.approve(item, client).status_code, 409)
        draft_path = "/api/spaces/sample/drafts/" + str(item["id"])
        detail = client.get(draft_path).json()
        self.assertEqual(detail["configuration_changes"], [])
        self.assertNotIn("acquaintance_date", detail["draft"]["policy_snapshot"])
        edited = self.write(draft_path, dict(expected_version=item["version"],
            expected_policy_revision=2, payload=item["payload"]), http=client)
        self.assertEqual(edited.status_code, 200, edited.text)
        actual = client.get(draft_path).json()
        self.assertEqual(actual["configuration_changes"], [])
        self.assertNotIn("acquaintance_date", actual["draft"]["policy_snapshot"])
        approved = self.approve(actual["draft"], client)
        self.assertEqual(approved.status_code, 200, approved.text)


class UpgradeTests(DatabaseTestCase):
    def test_v7_to_v8_preserves_configuration_cards_drafts_snapshots_and_sessions(self):
        install(self.owner, Settings(3), target_version=7)
        config = SpaceConfiguration(self.owner)
        config.configure("sample", Participant("小禾"), Participant("林舟"))
        item, _ = draft(self.app, "sample", "before-date")
        mid = PostgresStore(self.app, "sample").write_card("Synthetic existing card", [1, 0, 0])
        install_web(self.owner)
        with tempfile.TemporaryDirectory() as directory:
            password = Path(directory) / "password.hash"
            write_password(password, PASSWORD)
            token, _, _ = Sessions(self.app, password).login(PASSWORD, False)
            with connect(self.app) as connection:
                before_policy = connection.execute("SELECT * FROM memory_core.space_policies WHERE bot_id='sample'").fetchone()
                before_card = connection.execute("SELECT * FROM memory_core.memory_items WHERE id=%s", (mid,)).fetchone()
            self.assertTrue(install(self.owner, Settings(3)))
            self.assertFalse(install(self.owner, Settings(3)))
            self.assertFalse(install_web(self.owner))
            self.assertIsNotNone(Sessions(self.app, password).lookup(token))
            with connect(self.app) as connection:
                after_policy = connection.execute("SELECT * FROM memory_core.space_policies WHERE bot_id='sample'").fetchone()
                self.assertIsNone(after_policy.pop("acquaintance_date"))
                self.assertEqual(after_policy.pop("conversation_retention_days"), 30)
                self.assertEqual(after_policy.pop("persona"), "")
                self.assertTrue(after_policy.pop("auto_approve_enabled"))
                removed_columns = set(before_policy) - set(after_policy)
                self.assertEqual(len(removed_columns), 1)
                self.assertEqual(after_policy.pop("core_memory"), "\n".join(before_policy.pop(removed_columns.pop())))
                self.assertGreaterEqual(after_policy.pop("updated_at"), before_policy.pop("updated_at"))
                self.assertEqual(after_policy, before_policy)
                after_card = connection.execute("SELECT * FROM memory_core.memory_items WHERE id=%s", (mid,)).fetchone()
                self.assertEqual(after_card.pop("used_count"), 0)
                self.assertIsNone(after_card.pop("last_used_at"))
                self.assertIsNone(after_card.pop("restored_at"))
                self.assertEqual(after_card, before_card)
            after_draft = DraftStore(self.app, "sample").get(item["id"])
            self.assertIsNotNone(after_draft.pop("review_ready_at"))
            self.assertEqual(after_draft, item)
            self.assertNotIn("acquaintance_date", item["policy_snapshot"])
            settings = WebSettings(self.app, self.owner, ORIGIN, password)
            with TestClient(create_app(settings), base_url=ORIGIN) as client:
                client.cookies.set(settings.cookie, token)
                detail = client.get("/api/spaces/sample/drafts/" + str(item["id"])).json()
                self.assertIsNone(detail["configuration"]["acquaintance_date"])
                self.assertEqual(detail["configuration_changes"], [])
                SpaceConfiguration(self.owner).set_acquaintance_date("sample", expected_revision=1, acquaintance_date="2024-02-29")
                detail = client.get("/api/spaces/sample/drafts/" + str(item["id"])).json()
                self.assertEqual(detail["configuration_changes"], [])
                self.assertEqual(detail["draft"]["policy_snapshot"], item["policy_snapshot"])

    def test_v6_upgrade_preserves_drafts_and_only_reconstructs_matching_snapshots(self):
        install(self.owner, Settings(3), target_version=6)
        config = SpaceConfiguration(self.owner)
        config.configure("sample", Participant("小禾"), Participant("林舟"))
        old, _ = draft(self.app, "sample", "old")
        topic = old["payload"]["topic_ids"][0]
        config.edit_topic("sample", topic, expected_revision=1, name="学习计划")
        with connect(self.owner) as connection:
            connection.execute(
                "UPDATE memory_core.topics SET description=%s WHERE id=%s",
                ("升级前已有的主题说明", topic),
            )
        # The helper needs the original approved topic name to construct its fixed response.
        config.edit_topic("sample", topic, expected_revision=2, name="工作与学习")
        current, _ = draft(self.app, "sample", "current")
        install(self.owner, Settings(3))
        store = DraftStore(self.app, "sample")
        self.assertIsNone(store.get(old["id"])["policy_snapshot"])
        snapshot = store.get(current["id"])["policy_snapshot"]
        self.assertEqual(
            next(t["description"] for t in snapshot["topics"] if t["id"] == topic),
            "升级前已有的主题说明",
        )
        self.assertEqual(store.get(old["id"])["payload"], old["payload"])
        self.assertEqual(store.get(current["id"])["version"], current["version"])
        self.assertFalse(install(self.owner, Settings(3)))

    def test_each_supported_old_version_upgrades_before_web_install(self):
        # The earliest supported core baseline exercises every increment, with data.
        install(self.owner, Settings(3), target_version=1)
        store = PostgresStore(self.app, "legacy")
        mid = store.write_card("Synthetic legacy card", [1, 0, 0])
        for target in range(2, LATEST_VERSION + 1):
            install(self.owner, Settings(3), target_version=target)
        install_web(self.owner)
        self.assertFalse(install_web(self.owner))
        self.assertFalse(install(self.owner, Settings(3)))
        with connect(self.app) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT content FROM memory_core.memory_items WHERE id=%s", (mid,)
                ).fetchone()["content"],
                "Synthetic legacy card",
            )
