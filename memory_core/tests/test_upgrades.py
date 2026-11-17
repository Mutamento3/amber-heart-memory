from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import patch

import psycopg

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.storage.install import InstallationMismatch, MIGRATION, MIGRATIONS, catalog_fingerprint, install
from memory_core.storage.postgres import KeywordToken, PostgresStore
from memory_core.tests.support import DatabaseTestCase


class UpgradeTests(DatabaseTestCase):
    def legacy_data(self):
        install(self.owner, Settings(768), target_version=1)
        store = PostgresStore(self.app, "synthetic-upgrade")
        vector = [1] + [0] * 767
        card = store.write_card("合成旧数据", vector, [KeywordToken("旧词")])
        return store, card, vector

    def test_v1_data_and_sequence_survive_upgrade(self):
        store, card, vector = self.legacy_data()
        with connect(self.owner) as connection:
            before = connection.execute("SELECT * FROM memory_core.memory_items ORDER BY id").fetchall()
        self.assertTrue(install(self.owner, Settings(768), target_version=2))
        with connect(self.owner) as connection:
            self.assertEqual(connection.execute("SELECT * FROM memory_core.memory_items ORDER BY id").fetchall(), before)
            self.assertEqual([r["version"] for r in connection.execute("SELECT version FROM memory_core.schema_migrations ORDER BY version")], [1, 2])
            self.assertEqual(connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"], 2)
        self.assertEqual(store.search_keywords("旧词")[0]["memory_id"], card)
        self.assertEqual(store.search_vectors(vector)[0]["id"], card)
        self.assertGreater(store.write_card("合成新数据", vector), card)
        self.assertFalse(install(self.owner, Settings(768), target_version=2))

    def test_fresh_install_and_stepwise_upgrade_same_structure(self):
        install(self.owner, Settings(768), target_version=1)
        install(self.owner, Settings(768), target_version=2)
        with connect(self.owner) as connection:
            expected = catalog_fingerprint(connection)
            connection.execute("DROP SCHEMA memory_core CASCADE")
        install(self.owner, Settings(768), target_version=2)
        with connect(self.owner) as connection:
            self.assertEqual(catalog_fingerprint(connection), expected)

    def test_upgrade_failure_preserves_v1_and_data(self):
        store, card, _ = self.legacy_data()
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "002_migration_registry.sql"
            file.write_text((MIGRATIONS / file.name).read_text() + "\nSELECT 1/0;")
            with patch("memory_core.storage.install.MIGRATIONS", Path(directory)), self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner, Settings(768), target_version=2)
        with connect(self.owner) as connection:
            self.assertEqual(connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"], 1)
            self.assertIsNone(connection.execute("SELECT to_regclass('memory_core.schema_migrations') AS value").fetchone()["value"])
        self.assertEqual(store.search_keywords("旧词")[0]["memory_id"], card)
        self.assertTrue(install(self.owner, Settings(768), target_version=2))

    def test_applied_migration_edit_and_history_edit_are_rejected(self):
        install(self.owner, Settings(), target_version=2)
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "002_migration_registry.sql"
            file.write_text((MIGRATIONS / file.name).read_text() + "\n-- edited\n")
            with patch("memory_core.storage.install.MIGRATIONS", Path(directory)), self.assertRaises(InstallationMismatch):
                install(self.owner, Settings(), target_version=2)
        with connect(self.owner) as connection:
            connection.execute("DELETE FROM memory_core.schema_migrations WHERE version=1")
        with self.assertRaises(InstallationMismatch):
            install(self.owner, Settings(), target_version=2)

    def test_concurrent_upgrade_once_and_no_downgrade(self):
        self.legacy_data()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: install(self.owner, Settings(768), target_version=2), range(2)))
        self.assertCountEqual(results, [True, False])
        with self.assertRaises(InstallationMismatch):
            install(self.owner, Settings(768), target_version=1)

    def test_future_version_rejected_by_installer_and_runtime(self):
        install(self.owner, Settings(), target_version=2)
        with connect(self.owner) as connection:
            connection.execute("UPDATE memory_core.installation SET version=99")
        with self.assertRaises(InstallationMismatch):
            install(self.owner, Settings())
        with self.assertRaises(ValueError):
            PostgresStore(self.app, "synthetic")

    def test_baseline_001_has_not_changed(self):
        # This SHA belongs to the already accepted v1 baseline.
        self.assertEqual(hashlib.sha256(MIGRATION.read_bytes()).hexdigest(), "1b776068e97ab23df885381dc6622a74756e0abde461b6d4b06e2f6dcd4f0fa4")
