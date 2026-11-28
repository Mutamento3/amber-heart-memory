"""Legacy migration, storage constraints and extraction context, synthetic only."""

import re

import psycopg
from psycopg import sql

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.cards.drafts import context_for, context_digest
from memory_core.storage.install import install, MIGRATIONS
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase


# The migration owns the removed column name; legacy fixtures follow that SQL.
LEGACY_COLUMN = re.search(r"DROP COLUMN (\w+)", (MIGRATIONS / "021_core_memory.sql").read_text())[1]


class CoreMemoryUpgradeTests(DatabaseTestCase):
    def legacy(self, facts):
        install(self.owner, Settings(3), target_version=20)
        config = SpaceConfiguration(self.owner)
        config.configure('one', Participant('合成用户'), Participant('合成助手'))
        with connect(self.owner) as c:
            c.execute(sql.SQL("UPDATE memory_core.space_policies SET {}=%s WHERE bot_id='one'").format(sql.Identifier(LEGACY_COLUMN)), (facts,))
        return config

    def test_legacy_lines_preserved_repeat_install_and_current_sql_has_no_legacy_dependency(self):
        self.legacy(['第一条约定', '第二条\n手写续行'])
        self.assertTrue(install(self.owner, Settings(3)))
        self.assertFalse(install(self.owner, Settings(3)))
        with connect(self.app) as c:
            policy = c.execute('SELECT * FROM memory_core.space_policies').fetchone()
            self.assertEqual(policy['core_memory'], '第一条约定\n第二条\n手写续行')
            self.assertNotIn(LEGACY_COLUMN, policy)
            functions = c.execute("SELECT prosrc FROM pg_proc JOIN pg_namespace n ON pronamespace=n.oid WHERE n.nspname='memory_core'").fetchall()
            self.assertFalse(any(LEGACY_COLUMN in row['prosrc'] for row in functions))
            before = context_for(c, 'one')
        SpaceConfiguration(self.owner).set_core_memory('one', expected_revision=1, core_memory='更新后的约定')
        SpaceConfiguration(self.owner).set_persona('one', expected_revision=2, persona='更新后的人设')
        with connect(self.app) as c:
            after = context_for(c, 'one', include_persona=True)
        self.assertEqual(context_digest(before), context_digest(after))
        self.assertEqual(after['core_memory'], '更新后的约定')
        self.assertEqual(after['persona'], '更新后的人设')

    def test_oversized_legacy_text_stops_upgrade_without_losing_original(self):
        original = ['甲' * 3000, '乙' * 2000]
        self.legacy(original)
        with self.assertRaises(psycopg.errors.CheckViolation):
            install(self.owner, Settings(3))
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT version FROM memory_core.installation').fetchone()['version'], 20)
            self.assertEqual(c.execute('SELECT * FROM memory_core.space_policies').fetchone()[LEGACY_COLUMN], original)

    def test_unicode_limit_and_database_constraint(self):
        install(self.owner, Settings(3))
        config = SpaceConfiguration(self.owner)
        config.configure('one', Participant('合成用户'), Participant('合成助手'))
        text = '甲😀' * 2500
        self.assertEqual(config.set_core_memory('one', expected_revision=1, core_memory=text)['core_memory'], text)
        for content in (text + '乙', [], None):
            with self.assertRaises(ValueError):
                config.set_core_memory('one', expected_revision=2, core_memory=content)
        with self.assertRaises(psycopg.errors.CheckViolation), connect(self.owner) as c:
            c.execute("UPDATE memory_core.space_policies SET core_memory=%s WHERE bot_id='one'", (text + '乙',))
