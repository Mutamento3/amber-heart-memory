from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import psycopg

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.drafts import context_for
from memory_core.graph_store import GraphStore
from memory_core.install import MIGRATIONS, catalog_fingerprint, install
from memory_core.schema import LATEST_VERSION
from memory_core.space import Participant, SpaceConfiguration, read_topics, topic_presets
from memory_core.tests.support import DatabaseTestCase


class TopicPresetTests(DatabaseTestCase):
    def configure(self, template="lover", *, ignored=None):
        config = SpaceConfiguration(self.owner)
        args = {} if ignored is None else {"ignored_terms": ignored}
        return config.configure(template, Participant("小禾"), Participant("林舟"), topic_template=template, **args)

    def test_default_addresses_cli_override_and_omission_preserve_customization(self):
        install(self.owner, Settings(3))
        lover = self.configure()
        self.assertEqual(lover["ignored_terms"], ["宝宝", "宝贝", "亲爱的", "老公", "老婆", "Daddy"])
        self.assertEqual(self.configure("general")["ignored_terms"], [])
        config = SpaceConfiguration(self.owner)
        custom = config.configure("lover", Participant("小禾", ["禾禾"]), Participant("林舟"),
            expected_revision=1, ignored_terms=["自定义泛称"])
        saved = config.configure("lover", Participant("小禾", ["小禾苗"]), Participant("林舟"), expected_revision=2)
        self.assertEqual(saved["ignored_terms"], custom["ignored_terms"])
        cleared = config.configure("lover", Participant("小禾"), Participant("林舟"), expected_revision=3, ignored_terms=[])
        self.assertEqual(cleared["ignored_terms"], [])

    def test_description_is_live_template_data_and_role_names_are_substituted_once(self):
        install(self.owner, Settings(3))
        self.configure()
        with connect(self.app) as connection:
            rows = read_topics(connection, "lover")
            self.assertEqual([t["preset_key"] for t in rows], [t["key"] for t in topic_presets()["lover"]])
            columns = {r["column_name"] for r in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='memory_core' AND table_name='topics'")}
            self.assertNotIn("description", columns)
        updated = deepcopy(topic_presets())
        updated["lover"][0]["description"] = "用户和 AI 的模板新说明。普通的聊天、撒娇、陪伴不挂这个主题。"
        with patch("memory_core.space.topic_presets", return_value=updated):
            with connect(self.app) as connection:
                current = context_for(connection, "lover")
                self.assertEqual(current["topics"][0]["description"], "小禾和林舟的模板新说明。普通的聊天、撒娇、陪伴不挂这个主题。")
                self.assertEqual(connection.execute("SELECT revision FROM memory_core.space_policies WHERE bot_id='lover'").fetchone()["revision"], 1)

    def test_topic_identity_and_space_template_are_locked_even_for_owner(self):
        install(self.owner, Settings(3))
        self.configure()
        config = SpaceConfiguration(self.owner)
        topic = GraphStore(self.app, "lover").topics()[0]
        for field in ({"name": "改名"}, {"description": "改说明"}):
            with self.assertRaisesRegex(ValueError, "模板固定"):
                config.edit_topic("lover", topic["id"], expected_revision=1, **field)
        with self.assertRaisesRegex(ValueError, "锁定"):
            config.configure("lover", Participant("小禾"), Participant("林舟"), expected_revision=1, topic_template="general")
        for query in ("UPDATE memory_core.topics SET name='改名'", "UPDATE memory_core.topics SET preset_key='other'",
                      "DELETE FROM memory_core.topics", "UPDATE memory_core.space_policies SET topic_template='general'"):
            with self.subTest(query=query), self.assertRaises(psycopg.errors.RaiseException), connect(self.owner) as connection:
                connection.execute(query)

    def test_supported_old_versions_preserve_ids_links_and_custom_ignored_terms(self):
        install(self.owner, Settings(3), target_version=3)
        self.configure(ignored=["自定义泛称"])
        reused_config = SpaceConfiguration(self.owner)
        graph = GraphStore(self.app, "lover")
        topics = graph.topics()
        mid = graph.write_card("合成历史卡片", [1, 0, 0])
        graph.link_topic(mid, topics[0]["id"], primary=True)
        for version in range(4, LATEST_VERSION + 1):
            install(self.owner, Settings(3), target_version=version)
        self.assertFalse(install(self.owner, Settings(3)))
        reused_config.configure("after-upgrade", Participant("用户"), Participant("助手"))
        with connect(self.app) as connection:
            upgraded = read_topics(connection, "lover")
            self.assertEqual([t["id"] for t in upgraded], [t["id"] for t in topics])
            self.assertEqual(connection.execute("SELECT primary_topic_id FROM memory_core.memory_items WHERE id=%s", (mid,)).fetchone()["primary_topic_id"], topics[0]["id"])
            self.assertEqual(connection.execute("SELECT ignored_terms FROM memory_core.space_policies WHERE bot_id='lover'").fetchone()["ignored_terms"], ["自定义泛称"])

    def test_failed_migration_preserves_schema_topics_and_settings(self):
        install(self.owner, Settings(3), target_version=8)
        self.configure()
        with connect(self.owner) as connection:
            before = catalog_fingerprint(connection)
            topics = connection.execute("SELECT * FROM memory_core.topics ORDER BY id").fetchall()
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(directory) / "migrations"
            shutil.copytree(MIGRATIONS, migrations)
            file = migrations / "009_fixed_topic_presets.sql"
            file.write_text(file.read_text() + "\nSELECT 1/0;\n")
            with patch("memory_core.install.MIGRATIONS", migrations), self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner, Settings(3))
        with connect(self.owner) as connection:
            self.assertEqual(catalog_fingerprint(connection), before)
            self.assertEqual(connection.execute("SELECT * FROM memory_core.topics ORDER BY id").fetchall(), topics)
            self.assertEqual(connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"], 8)
        self.assertTrue(install(self.owner, Settings(3)))
