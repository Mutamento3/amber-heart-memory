from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import psycopg

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.graph_store import AmbiguousEntity, GraphStore
from memory_core.install import InstallationMismatch, MIGRATIONS, catalog_fingerprint, install
from memory_core.postgres import KeywordToken, PostgresStore
from memory_core.space import Participant, RevisionConflict, SpaceConfiguration, topic_presets
from memory_core.tests.support import DatabaseTestCase


class GraphConfigurationTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        install(self.owner, Settings(768))
        self.config = SpaceConfiguration(self.owner)
        self.user = Participant("Example User", ("User Alias",))
        self.assistant = Participant("Example Assistant", ("Assistant Alias",))
        self.policy = self.config.configure("space-one", self.user, self.assistant, ignored_terms=["dear"])
        self.graph = GraphStore(self.app, "space-one")
        self.vector = [1] + [0] * 767

    def test_participant_aliases_and_default_seed_exclusions(self):
        p = self.graph.policy()
        self.assertEqual(p["revision"], 1)
        self.assertIsNone(p["source_retention_days"])
        for name in (self.user.name, *self.user.aliases):
            self.assertEqual(self.graph.resolve_entity(name), p["user_entity_id"])
        for name in (self.assistant.name, *self.assistant.aliases):
            self.assertEqual(self.graph.resolve_entity(name), p["assistant_entity_id"])
        other = self.graph.create_entity("Other Person")
        candidates = [p["user_entity_id"], other, p["assistant_entity_id"], other]
        self.assertEqual(set(p["blocked_seed_entity_ids"]), {p["user_entity_id"], p["assistant_entity_id"]})
        self.assertEqual(self.graph.filter_graph_seeds(candidates), [other])
        with connect(self.app) as connection:
            self.assertEqual(connection.execute("SELECT memory_core.graph_seed_entities(%s,%s,%s) AS ids", ("space-one", candidates, 1)).fetchone()["ids"], [other])

    def test_address_only_terms_are_separate_and_do_not_hide_names(self):
        self.assertEqual(self.graph.clean_address_terms("dear Example User"), "Example User")
        self.assertEqual(self.graph.clean_address_terms("dearest User Alias"), "dearest User Alias")
        self.assertIsNone(self.graph.resolve_entity("dear"))
        with self.assertRaises(ValueError):
            self.config.configure("invalid", Participant("New User", ("dear",)), self.assistant, ignored_terms=["dear"])
        with self.assertRaises(psycopg.errors.RaiseException):
            self.graph.create_entity("dear")

    def test_distinct_participants_and_role_alias_overlap_rejected(self):
        with self.assertRaises(ValueError):
            self.config.configure("invalid", Participant("Same", ("Overlap",)), Participant("Other", ("OVERLAP",)))
        with self.assertRaises(psycopg.errors.RaiseException):
            self.graph.create_entity("User Alias")
        with connect(self.app) as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM memory_core.entities WHERE bot_id='space-one'").fetchone()["n"], 2)

    def test_normalization_and_ambiguous_names_without_blind_merge(self):
        first = self.graph.create_entity("《 Ｓｕｎ 》", aliases=["Solar"])
        self.assertEqual(self.graph.resolve_entity("sun"), first)
        self.assertEqual(self.graph.resolve_entity("ＳＯＬＡＲ"), first)
        for wrapped in ("【[ＳＵＮ]】", "'Sun'", "‘Sun’", "(Sun)"):
            self.assertEqual(self.graph.resolve_entity(wrapped), first)
        second = self.graph.create_entity("Sun", description="different synthetic object")
        self.assertNotEqual(first, second)
        with self.assertRaises(AmbiguousEntity): self.graph.resolve_entity("sun")

    def test_entity_key_concurrency_and_conflicting_retries(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(lambda _: self.graph.create_entity("Stable Object", entity_key="stable", embedding=self.vector), range(2)))
        self.assertEqual(ids[0], ids[1])
        with self.assertRaises(ValueError):
            self.graph.create_entity("Different Object", entity_key="stable")

    def test_policy_revision_preserves_participant_ids_and_rejects_stale_reads(self):
        before = self.graph.policy()
        after = self.config.configure("space-one", Participant("Renamed User", ("New Alias",)), self.assistant,
            ignored_terms=["dear"], source_retention_days=30, expected_revision=1)
        self.assertEqual(before["blocked_seed_entity_ids"], after["blocked_seed_entity_ids"])
        self.assertEqual(after["revision"], 2)
        self.assertEqual(after["source_retention_days"], 30)
        self.assertEqual(self.graph.resolve_entity("New Alias"), before["user_entity_id"])
        self.assertIsNone(self.graph.resolve_entity("User Alias"))
        with self.assertRaises(RevisionConflict):
            self.config.configure("space-one", self.user, self.assistant, expected_revision=1)
        with self.assertRaises(psycopg.errors.SerializationFailure):
            self.graph.filter_graph_seeds([], expected_revision=1)
        self.assertEqual(self.graph.filter_graph_seeds([]), [])

    def test_policy_update_can_atomically_move_a_term_between_categories(self):
        after = self.config.configure("space-one", Participant("Example User", ("dear",)), self.assistant,
            ignored_terms=[], expected_revision=1)
        self.assertEqual(after["revision"], 2)
        self.assertEqual(self.graph.resolve_entity("dear"), after["user_entity_id"])

    def test_concurrent_config_edits_one_wins_without_partial_changes(self):
        def edit(index):
            try:
                return self.config.configure("space-one", Participant(f"Renamed {index}"), self.assistant,
                    ignored_terms=["dear"], expected_revision=1)["revision"]
            except RevisionConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(edit, (1, 2)))
        self.assertCountEqual(results, [2, "conflict"])
        with connect(self.app) as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM memory_core.entities WHERE bot_id='space-one'").fetchone()["n"], 2)

    def test_both_fixed_topic_templates_and_stable_references(self):
        lover = self.graph.topics()
        self.assertEqual(len(lover), 11)
        self.assertEqual([t["name"] for t in lover], [t["name"] for t in topic_presets()["lover"]])
        self.assertEqual(sum(t["is_fallback"] for t in lover), 1)
        self.assertFalse(any(t["summary_eligible"] for t in lover))
        self.config.configure("space-two", self.user, self.assistant, topic_template="general")
        other = GraphStore(self.app, "space-two").topics()
        self.assertEqual([t["name"] for t in other], [t["name"] for t in topic_presets()["general"]])
        self.assertFalse(any(t["summary_eligible"] for t in other))
        card = self.graph.write_card("合成主题引用", self.vector)
        topic = lover[0]
        self.graph.link_topic(card, topic["id"], primary=True)
        self.config.edit_topic("space-one", topic["id"], expected_revision=1, summary_eligible=True)
        self.assertTrue(self.graph.topic_is_eligible(topic["id"]))
        with connect(self.app) as connection:
            row = connection.execute("SELECT primary_topic_id,is_milestone FROM memory_core.memory_items WHERE id=%s", (card,)).fetchone()
            self.assertEqual(row["primary_topic_id"], topic["id"])
            self.assertFalse(row["is_milestone"])
            self.assertTrue(connection.execute("SELECT summary_eligible FROM memory_core.topics WHERE id=%s", (topic["id"],)).fetchone()["summary_eligible"])
        with self.assertRaisesRegex(ValueError, "模板固定"):
            self.config.edit_topic("space-one", topic["id"], expected_revision=2, name=lover[1]["name"])
        self.assertEqual(self.graph.policy()["revision"], 2)
        fallback = self.graph.fallback_topic()
        with self.assertRaisesRegex(ValueError, "模板固定"):
            self.config.edit_topic("space-one", fallback["id"], expected_revision=2, name="Renamed fallback")
        self.assertEqual(self.graph.fallback_topic()["id"], fallback["id"])
        self.assertEqual(self.graph.fallback_topic()["name"], fallback["name"])

    def test_deferred_constraint_failure_closes_connection_and_rolls_back(self):
        connection = None
        with self.assertRaises(psycopg.errors.RaiseException):
            with connect(self.app) as connection:
                connection.execute("""INSERT INTO memory_core.entities(bot_id,entity_key,name,entity_type)
                    VALUES ('space-one','bad-deferred','dear','PERSON')""")
        self.assertTrue(connection.closed)
        self.assertIsNone(self.graph.resolve_entity("dear"))

    def test_cross_space_seeds_and_graph_foreign_keys_rejected(self):
        self.config.configure("space-two", self.user, self.assistant)
        other = GraphStore(self.app, "space-two")
        other_entity = other.create_entity("Other")
        entity = self.graph.create_entity("Local")
        card = self.graph.write_card("合成本地卡", self.vector)
        other_card = other.write_card("合成其他卡", self.vector)
        other_topic = other.topics()[0]["id"]
        for operation in (
            lambda: self.graph.filter_graph_seeds([other_entity]),
            lambda: self.graph.filter_graph_seeds([99999999]),
            lambda: self.graph.link_entity(card, other_entity),
            lambda: self.graph.link_entity(other_card, entity),
            lambda: self.graph.link_topic(card, other_topic, primary=True),
            lambda: self.graph.create_relation(entity, other_entity, "RELATES_TO", source_memory_id=card),
            lambda: self.graph.create_relation(entity, entity, "RELATES_TO", source_memory_id=other_card),
        ):
            with self.subTest(operation=operation), self.assertRaises(psycopg.Error): operation()
        with connect(self.app) as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM memory_core.memory_topics").fetchone()["n"], 0)
            self.assertIsNone(connection.execute("SELECT primary_topic_id FROM memory_core.memory_items WHERE id=%s", (card,)).fetchone()["primary_topic_id"])

    def test_relations_preserve_distinct_sources_and_event_constraint(self):
        one = self.graph.create_entity("Source")
        two = self.graph.create_entity("Target")
        event = self.graph.create_entity("Event", entity_type="EVENT")
        first_card = self.graph.write_card("合成来源一", self.vector)
        second_card = self.graph.write_card("合成来源二", self.vector)
        first = self.graph.create_relation(one, two, "RELATES_TO", source_memory_id=first_card, relation_key="r1")
        self.assertEqual(self.graph.create_relation(one, two, "RELATES_TO", source_memory_id=first_card, relation_key="r1"), first)
        second = self.graph.create_relation(one, two, "RELATES_TO", source_memory_id=second_card)
        self.assertNotEqual(first, second)
        with self.assertRaises(ValueError):
            self.graph.create_relation(one, two, "CAUSES", source_memory_id=first_card, relation_key="r1")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.graph.create_relation(one, two, "PARTICIPATES_IN", source_memory_id=first_card)
        self.graph.create_relation(one, event, "PARTICIPATES_IN", source_memory_id=first_card)
        self.assertEqual(self.graph.link_entity(first_card, one, mention_role="subject"), self.graph.link_entity(first_card, one))
        with connect(self.app) as connection:
            self.assertEqual(connection.execute("SELECT mention_role FROM memory_core.memory_entities WHERE memory_id=%s AND entity_id=%s", (first_card, one)).fetchone()["mention_role"], "subject")

    def test_application_cannot_modify_policy_topics_or_participant_entities(self):
        with self.assertRaises(ValueError): SpaceConfiguration(self.app)
        p = self.graph.policy()
        for query, params in (
            ("UPDATE memory_core.space_policies SET source_retention_days=1", ()),
            ("UPDATE memory_core.topics SET name='forbidden'", ()),
            ("UPDATE memory_core.entities SET name='forbidden' WHERE id=%s", (p["user_entity_id"],)),
            ("DELETE FROM memory_core.entities WHERE id=%s", (p["assistant_entity_id"],)),
            ("INSERT INTO memory_core.entities(bot_id,entity_key,name,entity_type) VALUES ('space-one','participant:fake','fake','PERSON')", ()),
        ):
            with self.subTest(query=query), self.assertRaises(psycopg.errors.InsufficientPrivilege), connect(self.app) as connection:
                connection.execute(query, params)

    def test_invalid_config_rolls_back_and_retention_requires_positive_days(self):
        for days in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.config.configure("bad", self.user, self.assistant, source_retention_days=days)
        with self.assertRaises(ValueError):
            self.config.configure("bad", self.user, self.assistant, topic_template="unknown")
        self.graph.create_entity("Existing Other")
        with self.assertRaises(psycopg.errors.RaiseException):
            self.config.configure("space-one", Participant("Existing Other"), self.assistant, expected_revision=1)
        self.assertEqual(self.graph.policy()["revision"], 1)
        self.assertEqual(self.graph.resolve_entity(self.user.name), self.policy["user_entity_id"])

    def test_default_cli_configuration_entry(self):
        env = {"PATH": os.environ["PATH"], "MEMORY_CONFIG_DSN": self.owner, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-m", "memory_core.space", "--space", "cli-space",
            "--user-name", "CLI User", "--user-alias", "CLI Alias", "--assistant-name", "CLI Assistant",
            "--ignored-term", "greeting", "--topic-template", "general"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["revision"], 1)
        self.assertEqual(len(GraphStore(self.app, "cli-space").topics()), 8)
        cleared = subprocess.run([sys.executable, "-m", "memory_core.space", "--space", "cli-space",
            "--user-name", "CLI User", "--assistant-name", "CLI Assistant", "--expected-revision", "1",
            "--clear-ignored-terms"], env=env, capture_output=True, text=True)
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(GraphStore(self.app, "cli-space").policy()["ignored_terms"], [])
        failure = subprocess.run([sys.executable, "-m", "memory_core.space", "--space", "cli-invalid",
            "--user-name", "[]", "--assistant-name", "PrivateNameFixture"], env=env, capture_output=True, text=True)
        self.assertEqual(failure.returncode, 2)
        self.assertNotIn("PrivateNameFixture", failure.stderr)
        self.assertNotIn("Failing row", failure.stderr)


class GraphMigrationTests(DatabaseTestCase):
    def test_legacy_data_preserved_while_upgrading_to_graph_schema(self):
        install(self.owner, Settings(768), target_version=1)
        store = PostgresStore(self.app, "old-space")
        vector = [1] + [0] * 767
        card = store.write_card("旧卡片", vector, [KeywordToken("保留")])
        install(self.owner, Settings(768))
        self.assertEqual(store.search_keywords("保留")[0]["memory_id"], card)
        config = SpaceConfiguration(self.owner)
        config.configure("old-space", Participant("Synthetic User"), Participant("Synthetic Assistant"))
        graph = GraphStore(self.app, "old-space")
        graph.link_topic(card, graph.topics()[0]["id"], primary=True)
        self.assertFalse(install(self.owner, Settings(768)))

    def test_install_template_selection_and_no_silent_reset(self):
        install(self.owner, Settings(768), topic_template="general")
        policy = SpaceConfiguration(self.owner).configure("selected", Participant("U"), Participant("A"))
        self.assertEqual(policy["topic_template"], "general")
        self.assertEqual(len(GraphStore(self.app, "selected").topics()), 8)
        self.assertFalse(install(self.owner, Settings(768)))
        with self.assertRaises(InstallationMismatch): install(self.owner, Settings(768), topic_template="lover")
        with self.assertRaises(ValueError): install(self.owner, Settings(768), target_version=2, topic_template="general")

    def test_fresh_and_sequential_graph_installs_have_same_catalog(self):
        install(self.owner, Settings(768), target_version=3)
        with connect(self.owner) as connection:
            fingerprint = catalog_fingerprint(connection)
            connection.execute("DROP SCHEMA memory_core CASCADE")
        for version in (1, 2, 3): install(self.owner, Settings(768), target_version=version)
        with connect(self.owner) as connection:
            self.assertEqual(catalog_fingerprint(connection), fingerprint)

    def test_graph_upgrade_failure_rolls_back_entire_version_sequence(self):
        install(self.owner, Settings(768), target_version=1)
        store = PostgresStore(self.app, "old-space")
        card = store.write_card("保留的升级材料", [1] + [0] * 767, [KeywordToken("保留")])
        with tempfile.TemporaryDirectory() as directory:
            for name in ("002_migration_registry.sql", "003_graph_and_space_policies.sql"):
                content = (MIGRATIONS / name).read_text()
                (Path(directory) / name).write_text(content + ("\nSELECT 1/0;" if name.startswith("003") else ""))
            with patch("memory_core.install.MIGRATIONS", Path(directory)), self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner, Settings(768), target_version=3)
        with connect(self.owner) as connection:
            self.assertEqual(connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"], 1)
            self.assertIsNone(connection.execute("SELECT to_regclass('memory_core.space_policies') AS value").fetchone()["value"])
        self.assertEqual(store.search_keywords("保留")[0]["memory_id"], card)
        self.assertTrue(install(self.owner, Settings(768)))
