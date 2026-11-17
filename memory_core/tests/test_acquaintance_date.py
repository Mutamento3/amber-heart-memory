from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import psycopg

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.install import MIGRATIONS, catalog_fingerprint, install
from memory_core.space import Participant, RevisionConflict, SpaceConfiguration, read_policy
from memory_core.tests.support import DatabaseTestCase


class AcquaintanceDateTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner, Settings(3))
        self.config = SpaceConfiguration(self.owner)
        self.user = Participant("Example User")
        self.assistant = Participant("Example Assistant")
        self.config.configure("example", self.user, self.assistant)

    def tearDown(self):
        pass

    def policy(self, space="example"):
        with connect(self.app) as connection:
            return read_policy(connection, space)

    def test_date_lifecycle_omission_preserves_value_and_spaces_are_independent(self):
        self.assertIsNone(self.policy()["acquaintance_date"])
        self.config.configure("other", self.user, self.assistant, acquaintance_date="2020-01-01")
        updated = self.config.set_acquaintance_date(
            "example", expected_revision=1, acquaintance_date="2024-02-29")
        self.assertEqual((updated["revision"], updated["acquaintance_date"]), (2, "2024-02-29"))
        updated = self.config.configure("example", self.user, self.assistant, expected_revision=2)
        self.assertEqual(updated["acquaintance_date"], "2024-02-29")
        self.assertEqual(self.policy("other")["acquaintance_date"], "2020-01-01")
        updated = self.config.configure("example", self.user, self.assistant,
            expected_revision=3, acquaintance_date=None)
        self.assertIsNone(updated["acquaintance_date"])
        # Calendar dates are not constrained to the current day or a timezone.
        updated = self.config.set_acquaintance_date(
            "example", expected_revision=4, acquaintance_date="9999-12-31")
        self.assertEqual(updated["acquaintance_date"], "9999-12-31")
        updated = self.config.set_acquaintance_date(
            "example", expected_revision=5, acquaintance_date=None)
        self.assertIsNone(updated["acquaintance_date"])

    def test_invalid_dates_rejected_by_service_and_database_without_mutation(self):
        invalid = ("", "2023-02-29", "1900-02-29", "2024-04-31", "2024-13-01",
            "2024-2-09", "2024-02-29 ", "2024-02-29T00:00:00+08:00", "0000-01-01",
            "２０２４-０２-２９", "tomorrow")
        for value in (*invalid, True, 20240229, date(2024, 2, 29)):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "相识日"):
                self.config.set_acquaintance_date("example", expected_revision=1, acquaintance_date=value)
        # The owner path cannot bypass the final calendar/format constraint either.
        for value in invalid:
            with self.subTest(sql_value=value), self.assertRaises(psycopg.Error), connect(self.owner) as connection:
                connection.execute("UPDATE memory_core.space_policies SET acquaintance_date=%s WHERE bot_id='example'", (value,))
        self.assertEqual(self.policy()["revision"], 1)
        self.assertIsNone(self.policy()["acquaintance_date"])

    def test_concurrent_and_stale_updates_cannot_overwrite_configuration(self):
        def update(value):
            try:
                self.config.set_acquaintance_date("example", expected_revision=1, acquaintance_date=value)
                return "saved"
            except RevisionConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ("2024-02-29", "2023-01-01")))
        self.assertCountEqual(results, ["saved", "conflict"])
        saved = self.policy()
        with self.assertRaises(RevisionConflict):
            self.config.configure("example", Participant("Stale User"), self.assistant,
                expected_revision=1, acquaintance_date=None)
        self.assertEqual(self.policy(), saved)
        with self.assertRaises(ValueError):
            self.config.set_acquaintance_date("missing", expected_revision=0, acquaintance_date=None)

    def test_participants_and_date_roll_back_together_on_late_failure(self):
        before = self.policy()
        with patch("memory_core.space.read_policy", side_effect=RuntimeError("injected after writes")), self.assertRaises(RuntimeError):
            self.config.configure("example", Participant("Renamed User"), self.assistant,
                expected_revision=1, acquaintance_date="2024-02-29")
        self.assertEqual(self.policy(), before)

    def test_application_can_read_date_and_snapshot_but_cannot_write_it(self):
        self.config.set_acquaintance_date("example", expected_revision=1, acquaintance_date="2024-02-29")
        with connect(self.app) as connection:
            snapshot = connection.execute("SELECT memory_core.configuration_snapshot('example') AS value").fetchone()["value"]
            self.assertEqual(set(snapshot), {"participants"})
        with self.assertRaises(psycopg.errors.InsufficientPrivilege), connect(self.app) as connection:
            connection.execute("UPDATE memory_core.space_policies SET acquaintance_date=NULL WHERE bot_id='example'")
        self.assertEqual(self.policy()["acquaintance_date"], "2024-02-29")


class AcquaintanceMigrationTests(DatabaseTestCase):
    def test_v7_compatibility_and_failed_v8_migration_roll_back_fully(self):
        install(self.owner, Settings(3), target_version=7)
        config = SpaceConfiguration(self.owner)
        config.configure("legacy", Participant("Example User"), Participant("Example Assistant"))
        # Existing callers remain valid, while a new field needs the migration.
        config.configure("legacy", Participant("Example User"), Participant("Example Assistant"), expected_revision=1)
        with self.assertRaisesRegex(ValueError, "版本 8"):
            config.set_acquaintance_date("legacy", expected_revision=2, acquaintance_date=None)
        with self.assertRaisesRegex(ValueError, "版本 8"):
            config.configure("new", Participant("User"), Participant("Assistant"), acquaintance_date="2024-02-29")
        with connect(self.owner) as connection:
            before = catalog_fingerprint(connection)
            policy = read_policy(connection, "legacy")
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(directory) / "migrations"
            shutil.copytree(MIGRATIONS, migrations)
            path = migrations / "008_space_acquaintance_date.sql"
            path.write_text(path.read_text() + "\nSELECT 1/0;\n")
            with patch("memory_core.install.MIGRATIONS", migrations), self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner, Settings(3))
        with connect(self.owner) as connection:
            self.assertEqual(catalog_fingerprint(connection), before)
            self.assertEqual(read_policy(connection, "legacy"), policy)
            self.assertEqual(connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"], 7)
            self.assertEqual(connection.execute("SELECT max(version) AS v FROM memory_core.schema_migrations").fetchone()["v"], 7)
        self.assertTrue(install(self.owner, Settings(3)))
        self.assertFalse(install(self.owner, Settings(3)))
        with connect(self.app) as connection:
            self.assertIsNone(read_policy(connection, "legacy")["acquaintance_date"])
