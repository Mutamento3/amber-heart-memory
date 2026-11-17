from datetime import datetime, timezone
import os
import subprocess
import sys

import psycopg

from memory_core.config import Settings
from memory_core.daily_tasks import DailyTasks
from memory_core.db import connect
from memory_core.install import install, catalog_fingerprint, InstallationMismatch
from memory_core.space import Participant, SpaceConfiguration, RevisionConflict
from memory_core.tests.support import DatabaseTestCase


class DailyCardMigrationTests(DatabaseTestCase):
    def test_v13_upgrade_defaults_revision_usage_and_repeat(self):
        install(self.owner, Settings(3), target_version=13)
        config = SpaceConfiguration(self.owner)
        config.configure("one", Participant("合成用户"), Participant("合成助手"))
        install(self.owner, Settings(3),target_version=14)
        config = SpaceConfiguration(self.owner)
        self.assertEqual(
            config.set_review_mode("one", expected_revision=1, review_mode="manual")[
                "revision"
            ],
            2,
        )
        with self.assertRaises(RevisionConflict):
            config.set_review_mode("one", expected_revision=1, review_mode="auto")
        tasks = DailyTasks(self.app, "one")
        now = datetime.now(timezone.utc)
        with tasks.run_lock() as acquired:
            self.assertTrue(acquired)
            task = tasks.claim("extraction", "2026-11-01", now=now)
            tasks.record_usage(
                task,
                dict(
                    stage="selection",
                    model="synthetic",
                    state="succeeded",
                    usage={"total_tokens": 12},
                ),
                now=now,
            )
            tasks.progress(task, dict(extraction_id=42))
            tasks.fail(task, "synthetic_failure", now=now)
        self.assertFalse(install(self.owner, Settings(3),target_version=14))
        with connect(self.app) as c:
            row = c.execute("SELECT * FROM memory_core.daily_tasks").fetchone()
            self.assertEqual(len(row["model_usage"]), 1)
            self.assertEqual(row["result"]["extraction_id"], 42)
            self.assertEqual(
                c.execute(
                    "SELECT review_mode FROM memory_core.space_policies"
                ).fetchone()["review_mode"],
                "manual",
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE memory_core.space_policies SET review_mode='auto'")

    def test_legacy_sources_abort_entire_upgrade_without_relabeling(self):
        install(self.owner, Settings(3), target_version=13)
        SpaceConfiguration(self.owner).configure(
            "one", Participant("合成用户"), Participant("合成助手")
        )
        with connect(self.owner) as c:
            c.execute(
                "INSERT INTO memory_core.source_materials(bot_id,source_key,content_sha256,business_date,source_kind,segments) VALUES ('one','legacy','synthetic','2026-11-01','chatroom','[]')"
            )
            before = catalog_fingerprint(c)
        with self.assertRaisesRegex(InstallationMismatch, "原数据保留"):
            install(self.owner, Settings(3))
        env = {
            k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "PYTHONPATH")
        }
        env["MEMORY_INSTALL_DSN"] = self.owner
        result = subprocess.run(
            [sys.executable, "-m", "memory_core.install", "--dimensions", "3"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("原数据保留", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        with connect(self.owner) as c:
            self.assertEqual(catalog_fingerprint(c), before)
            self.assertEqual(
                c.execute("SELECT version FROM memory_core.installation").fetchone()[
                    "version"
                ],
                13,
            )
            self.assertEqual(
                c.execute(
                    "SELECT source_kind FROM memory_core.source_materials"
                ).fetchone()["source_kind"],
                "chatroom",
            )

    def test_fresh_source_constraints_and_single_public_setup(self):
        install(self.owner, Settings(3))
        config = SpaceConfiguration(self.owner)
        config.configure(
            "one", Participant("合成用户"), Participant("合成助手"), single_space=True
        )
        with self.assertRaisesRegex(ValueError, "一套记忆库只服务一个"):
            config.configure(
                "two",
                Participant("另一个用户"),
                Participant("另一个助手"),
                single_space=True,
            )
        # Core partitioning remains available to trusted callers and tests.
        config.configure("two", Participant("另一个用户"), Participant("另一个助手"))
        with connect(self.app) as c:
            with self.assertRaises(psycopg.errors.CheckViolation):
                c.execute(
                    "INSERT INTO memory_core.memory_items(bot_id,content,source_kind) VALUES ('one','synthetic','coreading')"
                )
