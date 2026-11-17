"""Integration tests: real isolated Postgres, synthetic vectors, no model clients."""

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.install import InstallationMismatch, catalog_fingerprint, install
from memory_core.postgres import KeywordToken, PostgresStore


class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fail, rather than skip, when the isolated DB harness is absent.
        cls.admin = os.environ["MEMORY_TEST_ADMIN_DSN"]
        cls.owner = os.environ["MEMORY_TEST_OWNER_DSN"]
        cls.application = os.environ["MEMORY_TEST_APP_DSN"]
        cls.databases = []
        for dimension in (1536, 768, 2000):
            name = f"memory_test_{dimension}"
            cls.create_database(name)
            owner = make_conninfo(cls.owner, dbname=name)
            assert install(owner, Settings(dimension))
        cls.create_database("memory_test_scratch")
        cls.owner1536 = make_conninfo(cls.owner, dbname="memory_test_1536")
        cls.app1536 = make_conninfo(cls.application, dbname="memory_test_1536")

    @classmethod
    def create_database(cls, name):
        with psycopg.connect(cls.admin, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER memory_owner").format(sql.Identifier(name)))
        cls.databases.append(name)
        with psycopg.connect(make_conninfo(cls.admin, dbname=name), autocommit=True) as admin:
            admin.execute("CREATE EXTENSION vector WITH SCHEMA public")
            admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
            admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO memory_app").format(sql.Identifier(name)))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.admin, autocommit=True) as admin:
            for name in cls.databases:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

    def setUp(self):
        self.space = "synthetic_" + uuid4().hex
        self.store = PostgresStore(self.app1536, self.space)
        self.vector = [1.0] + [0.0] * 1535

    def count_cards(self):
        with connect(self.app1536) as connection:
            return connection.execute("SELECT count(*) AS count FROM memory_core.memory_items WHERE bot_id=%s", (self.space,)).fetchone()["count"]

    def test_write_and_both_retrieval_paths(self):
        first = self.store.write_card("合成测试：植物笔记", self.vector, [KeywordToken("植物", "title")], title="植物")
        self.store.write_card("合成测试：交通笔记", [0, 1] + [0] * 1534, [KeywordToken("交通")])
        result = self.store.search_vectors(self.vector, limit=1)
        self.assertEqual(result[0]["id"], first)
        self.assertAlmostEqual(result[0]["similarity"], 1.0)
        hits = self.store.search_keywords("植物 OR 其他")
        self.assertEqual([row["memory_id"] for row in hits], [first])
        self.assertEqual(hits[0]["keyword_field"], "title")
        self.assertGreater(hits[0]["raw_score"], 0)

    def test_nondefault_dimension_and_upper_limit(self):
        for dimension in (768, 2000):
            with self.subTest(dimension=dimension):
                store = PostgresStore(make_conninfo(self.application, dbname=f"memory_test_{dimension}"), self.space)
                vector = [1] + [0] * (dimension - 1)
                card = store.write_card("合成维度测试", vector, [KeywordToken("维度")])
                self.assertEqual(store.search_vectors(vector)[0]["id"], card)
                self.assertEqual(store.search_keywords("维度")[0]["memory_id"], card)

    def test_invalid_vector_and_search_options(self):
        for value in ([1], [0] * 1536, [float("nan")] + [0] * 1535, [float("inf")] + [0] * 1535):
            with self.assertRaises(ValueError):
                self.store.write_card("不能写入", value)
        for options in ({"limit": -1}, {"limit": True}, {"item_kind": "unknown"}, {"include_archived": "false"}):
            with self.assertRaises(ValueError):
                self.store.search_keywords("测试", **options)
        self.assertEqual(self.count_cards(), 0)

    def test_card_and_keywords_rollback_together(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.store.write_card("必须回滚", self.vector, [KeywordToken("词条", term_frequency=-1)])
        self.assertEqual(self.count_cards(), 0)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.store.write_card("重复词条也必须回滚", self.vector, [KeywordToken("重复"), KeywordToken("重复")])
        self.assertEqual(self.count_cards(), 0)

    def test_keyword_replacement_rollback_and_idempotence(self):
        card = self.store.write_card("合成卡片", self.vector, [KeywordToken("原词")])
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.store.replace_keywords(card, [KeywordToken("新词"), KeywordToken("新词")])
        self.assertEqual(self.store.search_keywords("原词")[0]["memory_id"], card)
        self.assertEqual(self.store.search_keywords("新词"), [])
        for _ in range(2):
            self.assertEqual(self.store.replace_keywords(card, [KeywordToken("新词")]), 1)
        self.assertEqual(self.store.search_keywords("原词"), [])
        self.assertEqual(len(self.store.search_keywords("新词")), 1)

    def test_concurrent_keyword_replacements_do_not_mix_sets(self):
        card = self.store.write_card("并发合成卡片", self.vector)
        groups = [[KeywordToken(f"词组{index}甲"), KeywordToken(f"词组{index}乙")] for index in (1, 2)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda tokens: self.store.replace_keywords(card, tokens), groups))
        self.assertEqual(results, [2, 2])
        with connect(self.app1536) as connection:
            terms = {r["term"] for r in connection.execute("SELECT term FROM memory_core.memory_keyword_tokens WHERE memory_id=%s", (card,))}
        self.assertIn(terms, [{token.term for token in group} for group in groups])

    def test_concurrent_first_installs_are_serialized(self):
        dsn = make_conninfo(self.owner, dbname="memory_test_scratch")
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: install(dsn, Settings()), range(2)))
            self.assertCountEqual(results, [True, False])
            self.assertFalse(install(dsn, Settings()))
        finally:
            with connect(dsn) as connection:
                connection.execute("DROP SCHEMA IF EXISTS memory_core CASCADE")

    def test_two_spaces_and_cross_space_keyword_write(self):
        card = self.store.write_card("空间甲", self.vector, [KeywordToken("共享词")])
        other = PostgresStore(self.app1536, self.space + "_other")
        other_card = other.write_card("空间乙", self.vector, [KeywordToken("共享词")])
        self.assertEqual([r["id"] for r in self.store.search_vectors(self.vector)], [card])
        self.assertEqual([r["memory_id"] for r in other.search_keywords("共享词")], [other_card])
        with self.assertRaises(psycopg.errors.RaiseException):
            other.replace_keywords(card, [KeywordToken("越界")])
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), connect(self.app1536) as connection:
            connection.execute("""INSERT INTO memory_core.memory_keyword_tokens VALUES
                (%s,%s,'content',1,'越界',1,1,'2026-06-12T03:00:00+08:00')""", (other.space_id, card))
        self.assertEqual(self.store.search_keywords("越界"), [])

    def test_archive_and_item_kind_scopes(self):
        live = self.store.write_card("未归档", self.vector, [KeywordToken("共同词")])
        archived = self.store.write_card("已归档", self.vector, [KeywordToken("共同词")])
        summary = self.store.write_card("总结", self.vector, [KeywordToken("共同词")], item_kind="topic_summary")
        with connect(self.app1536) as connection:
            connection.execute("UPDATE memory_core.memory_items SET archive_reason='decay', archived_at=created_at WHERE id=%s", (archived,))
        self.assertEqual([r["id"] for r in self.store.search_vectors(self.vector)], [live])
        self.assertEqual([r["memory_id"] for r in self.store.search_keywords("共同词")], [live])
        self.assertEqual({r["id"] for r in self.store.search_vectors(self.vector, include_archived=True, item_kind="all")}, {live, archived, summary})
        self.assertEqual([r["memory_id"] for r in self.store.search_keywords("共同词", item_kind="topic_summary")], [summary])

    def test_parameterized_keywords_and_space(self):
        self.store.write_card("安全合成样本", self.vector, [KeywordToken("样本")])
        self.assertEqual(self.store.search_keywords("'; DROP TABLE memory_core.memory_items; --"), [])
        unusual = PostgresStore(self.app1536, "space'; --")
        card = unusual.write_card("特殊空间名", self.vector, [KeywordToken("样本")])
        self.assertEqual(unusual.search_keywords("样本")[0]["memory_id"], card)
        self.assertEqual(self.count_cards(), 1)

    def test_repeat_install_preserves_rows_definitions_and_sequence(self):
        first = self.store.write_card("保留的数据", self.vector, [KeywordToken("保留")])
        with connect(self.owner1536) as connection:
            before = catalog_fingerprint(connection)
        self.assertFalse(install(self.owner1536, Settings()))
        self.assertEqual(self.store.search_keywords("保留")[0]["memory_id"], first)
        second = self.store.write_card("继续写入", self.vector)
        self.assertGreater(second, first)
        with connect(self.owner1536) as connection:
            self.assertEqual(catalog_fingerprint(connection), before)

    def test_changed_config_stops_without_changing_existing_data(self):
        card = self.store.write_card("不能丢", self.vector, [KeywordToken("保留")])
        for settings in (Settings(768), Settings(timezone="UTC"), Settings(day_start_hour=5)):
            with self.assertRaises(InstallationMismatch):
                install(self.owner1536, settings)
        self.assertEqual(self.store.search_keywords("保留")[0]["memory_id"], card)
        self.assertFalse(install(self.owner1536, Settings()))

    def test_schema_drift_is_rejected(self):
        with connect(self.owner1536) as connection:
            connection.execute("ALTER TABLE memory_core.memory_items ADD COLUMN unexpected text")
        try:
            with self.assertRaises(InstallationMismatch):
                install(self.owner1536, Settings())
        finally:
            with connect(self.owner1536) as connection:
                connection.execute("ALTER TABLE memory_core.memory_items DROP COLUMN unexpected")
        self.assertFalse(install(self.owner1536, Settings()))

    def test_permission_drift_is_rejected(self):
        with connect(self.owner1536) as connection:
            connection.execute("REVOKE SELECT ON memory_core.memory_items FROM memory_app")
        try:
            with self.assertRaises(InstallationMismatch):
                install(self.owner1536, Settings())
        finally:
            with connect(self.owner1536) as connection:
                connection.execute("GRANT SELECT ON memory_core.memory_items TO memory_app")
        self.assertFalse(install(self.owner1536, Settings()))

    def test_migration_edit_is_rejected(self):
        from memory_core.install import MIGRATION
        with tempfile.TemporaryDirectory() as directory:
            migration = Path(directory) / "001.sql"
            migration.write_text(MIGRATION.read_text() + "\n-- changed migration\n")
            with patch("memory_core.install.MIGRATION", migration), self.assertRaises(InstallationMismatch):
                install(self.owner1536, Settings())

    def test_empty_install_failure_rolls_back_everything(self):
        from memory_core.install import MIGRATION
        dsn = make_conninfo(self.owner, dbname="memory_test_scratch")
        with tempfile.TemporaryDirectory() as directory:
            migration = Path(directory) / "001.sql"
            migration.write_text(MIGRATION.read_text() + "\nSELECT 1/0;\n")
            with patch("memory_core.install.MIGRATION", migration), self.assertRaises(psycopg.errors.DivisionByZero):
                install(dsn, Settings())
        with connect(dsn) as connection:
            self.assertIsNone(connection.execute("SELECT to_regnamespace('memory_core') AS value").fetchone()["value"])

    def test_rejects_unversioned_schema(self):
        dsn = make_conninfo(self.owner, dbname="memory_test_scratch")
        with connect(dsn) as connection:
            connection.execute("CREATE SCHEMA memory_core")
        try:
            with self.assertRaises(InstallationMismatch):
                install(dsn, Settings())
        finally:
            with connect(dsn) as connection:
                connection.execute("DROP SCHEMA memory_core")

    def test_real_cli_and_default_store_entry(self):
        env = {"PATH": os.environ["PATH"], "MEMORY_INSTALL_DSN": self.owner1536, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-m", "memory_core.install"], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("保留已有数据", result.stdout)
        with patch.dict(os.environ, {"MEMORY_DSN": self.app1536}, clear=True):
            store = PostgresStore.from_env(self.space)
            self.assertEqual(store.settings, Settings())
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            PostgresStore.from_env(self.space)

    def test_schema_types_indexes_constraints_trigger_and_permissions(self):
        with connect(self.owner1536) as connection:
            columns = connection.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod) AS type,
                a.attnotnull,a.attidentity FROM pg_attribute a
                WHERE a.attrelid='memory_core.memory_items'::regclass AND a.attnum>0 AND NOT a.attisdropped""").fetchall()
            columns = {row["attname"]: row for row in columns}
            self.assertEqual(columns["id"]["type"], "integer")
            self.assertEqual(columns["id"]["attidentity"], "d")
            self.assertEqual(columns["embedding"]["type"], "vector(1536)")
            self.assertTrue(columns["bot_id"]["attnotnull"])
            self.assertEqual(columns["created_at"]["type"], "text")
            expected = {
                "id": ("integer", True), "bot_id": ("text", True), "content": ("text", True),
                "summary": ("text", False), "title": ("text", False), "item_kind": ("text", True),
                "source_date": ("text", False), "embedding": ("vector(1536)", False),
                "archive_reason": ("text", False), "archived_at": ("text", False),
                "created_at": ("text", True), "updated_at": ("text", False),
                "is_milestone": ("boolean", True), "decay_layer": ("text", True),
                "temperature": ("text", True), "cognitive_status": ("text", True),
                "valence": ("real", False), "arousal": ("real", False),
                "source_type": ("text", True), "source_kind": ("text", False),
                "experience_mode": ("text", False), "primary_topic_id": ("integer", False),
                "parent_reflection_id": ("integer", False), "child_episode_ids": ("jsonb", False),
                "gardener_result": ("text", False),
                "used_count": ("bigint", True), "last_used_at": ("text", False),
                "restored_at": ("text", False),
            }
            self.assertEqual({key: (row["type"], row["attnotnull"]) for key, row in columns.items()}, expected)
            keyword_columns = connection.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull
                FROM pg_attribute a WHERE a.attrelid='memory_core.memory_keyword_tokens'::regclass
                AND a.attnum>0 AND NOT a.attisdropped""").fetchall()
            expected = {key: ("integer" if key in ("memory_id", "source_id", "term_frequency", "field_length") else "text", True)
                for key in ("bot_id", "memory_id", "field", "source_id", "term", "term_frequency", "field_length", "updated_at")}
            self.assertEqual({row["attname"]: (row["type"], row["attnotnull"]) for row in keyword_columns}, expected)
            index = connection.execute("SELECT indexdef FROM pg_indexes WHERE indexname='memory_items_vector'").fetchone()["indexdef"]
            self.assertIn("USING hnsw", index)
            self.assertIn("vector_cosine_ops", index)
            archive_index = connection.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='memory_core' AND indexname='memory_items_archive_month'").fetchone()['indexdef']
            self.assertIn('(bot_id, archived_at)', archive_index)
            self.assertIn('archive_reason IS NOT NULL', archive_index)
            self.assertEqual(connection.execute("SELECT count(*) AS value FROM pg_trigger WHERE tgrelid='memory_core.memory_items'::regclass AND NOT tgisinternal").fetchone()["value"], 1)
            self.assertEqual(connection.execute("SELECT count(*) AS value FROM pg_constraint WHERE conrelid='memory_core.memory_keyword_tokens'::regclass AND contype='f'").fetchone()["value"], 1)
            foreign_key = connection.execute("""SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint
                WHERE conrelid='memory_core.memory_keyword_tokens'::regclass AND contype='f'""").fetchone()["definition"]
            self.assertIn("FOREIGN KEY (memory_id, bot_id)", foreign_key)
            self.assertIn("ON DELETE CASCADE", foreign_key)
            self.assertIsNotNone(connection.execute("SELECT pg_get_serial_sequence('memory_core.memory_items','id') AS value").fetchone()["value"])
            public_functions = connection.execute("""SELECT p.proname FROM pg_proc p
                JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='memory_core'
                AND (p.prosecdef OR has_function_privilege('memory_outsider',p.oid,'EXECUTE'))""").fetchall()
            self.assertEqual(public_functions, [])
            self.assertFalse(connection.execute("SELECT relrowsecurity AS value FROM pg_class WHERE oid='memory_core.memory_items'::regclass").fetchone()["value"])
        for statement in (
            "CREATE TABLE memory_core.forbidden(id integer)",
            "UPDATE memory_core.installation SET catalog_sha256='modified'",
            "ALTER TABLE memory_core.memory_items ADD COLUMN forbidden text",
            "TRUNCATE memory_core.memory_items",
        ):
            with self.subTest(statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege), connect(self.app1536) as connection:
                connection.execute(statement)
        with self.assertRaises(ValueError):
            PostgresStore(self.owner1536, self.space)
        with psycopg.connect(make_conninfo(self.admin, dbname="memory_test_1536")) as connection:
            connection.execute("SET ROLE memory_outsider")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM memory_core.memory_items")

    def test_semantic_trigger_and_offset_timestamps(self):
        card = self.store.write_card("原合成正文", self.vector)
        with connect(self.app1536) as connection:
            before = connection.execute("SELECT updated_at FROM memory_core.memory_items WHERE id=%s", (card,)).fetchone()["updated_at"]
            connection.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=created_at WHERE id=%s", (card,))
            after = connection.execute("SELECT updated_at FROM memory_core.memory_items WHERE id=%s", (card,)).fetchone()["updated_at"]
            self.assertEqual(before, after)
            connection.execute("UPDATE memory_core.memory_items SET content='修改后',updated_at='2000-01-01T00:00:00+00:00' WHERE id=%s", (card,))
            timestamp = connection.execute("SELECT updated_at FROM memory_core.memory_items WHERE id=%s", (card,)).fetchone()["updated_at"]
            self.assertTrue(timestamp.endswith("+08:00"))
            self.assertGreater(datetime.fromisoformat(timestamp).year, 2000)

    def test_nondefault_timezone_used_in_database_trigger(self):
        dsn = make_conninfo(self.owner, dbname="memory_test_scratch")
        app = make_conninfo(self.application, dbname="memory_test_scratch")
        try:
            install(dsn, Settings(768, "America/New_York", 5))
            store = PostgresStore(app, self.space)
            card = store.write_card("合成时区", [1] + [0] * 767)
            with connect(app) as connection:
                timestamp = connection.execute("SELECT created_at FROM memory_core.memory_items WHERE id=%s", (card,)).fetchone()["created_at"]
                self.assertIn(timestamp[-6:], ("-04:00", "-05:00"))
                connection.execute("UPDATE memory_core.memory_items SET title='修改' WHERE id=%s", (card,))
                timestamp = connection.execute("SELECT updated_at FROM memory_core.memory_items WHERE id=%s", (card,)).fetchone()["updated_at"]
                self.assertIn(timestamp[-6:], ("-04:00", "-05:00"))
        finally:
            with connect(dsn) as connection:
                connection.execute("DROP SCHEMA IF EXISTS memory_core CASCADE")

    def test_candidate_public_subset_has_no_private_runtime(self):
        self.assertEqual({path.name for path in Path.cwd().iterdir()}, {"memory_core"})
        self.assertIsNone(importlib.util.find_spec("supabase"))
        self.assertIsNone(importlib.util.find_spec("openai"))
        self.assertIsNone(importlib.util.find_spec("config"))
        for name in ("SUPABASE_URL", "SUPABASE_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
            self.assertNotIn(name, os.environ)


if __name__ == "__main__":
    unittest.main()
