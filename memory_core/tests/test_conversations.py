from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch

import psycopg
from psycopg.types.json import Jsonb

from memory_core.config import Settings
from memory_core.conversations import ConversationStore, DuplicateRequest
from memory_core.db import connect
from memory_core.install import install, catalog_fingerprint, MIGRATIONS
from memory_core.space import SpaceConfiguration, Participant
from memory_core.tests.support import DatabaseTestCase


BODY = {'model': 'synthetic-model', 'messages': [{'role': 'user', 'content': '合成问句'}]}


class ConversationTests(DatabaseTestCase):
    def ready(self, settings=None):
        self.settings = settings or Settings(3)
        install(self.owner, self.settings)
        config = SpaceConfiguration(self.owner)
        for space in ('one', 'two'):
            config.configure(space, Participant('用户'), Participant('助手'))
        self.store = ConversationStore(self.app, 'one')

    def test_v9_upgrade_repeat_and_failure_are_atomic(self):
        settings = Settings(3)
        install(self.owner, settings, target_version=9)
        SpaceConfiguration(self.owner).configure('old', Participant('用户'), Participant('助手'), source_retention_days=None)
        with connect(self.owner) as c:
            old = c.execute('SELECT * FROM memory_core.space_policies').fetchone()
        with tempfile.TemporaryDirectory() as directory:
            migration = Path(directory) / '010_conversations.sql'
            migration.write_text((MIGRATIONS / migration.name).read_text() + '\nSELECT 1/0;')
            for source in MIGRATIONS.glob('*.sql'):
                if source.name != migration.name:
                    (Path(directory) / source.name).write_bytes(source.read_bytes())
            with patch('memory_core.install.MIGRATIONS', Path(directory)), self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner, settings)
        with connect(self.owner) as c:
            self.assertEqual(c.execute('SELECT version FROM memory_core.installation').fetchone()['version'], 9)
            self.assertIsNone(c.execute("SELECT to_regclass('memory_core.conversation_turns') AS t").fetchone()['t'])
        self.assertTrue(install(self.owner, settings))
        with connect(self.owner) as c:
            row = c.execute('SELECT * FROM memory_core.space_policies').fetchone()
            removed_columns = set(old) - set(row)
            self.assertEqual(len(removed_columns), 1)
            self.assertEqual(row['core_memory'], '\n'.join(old.pop(removed_columns.pop())))
            self.assertGreaterEqual(row['updated_at'], old.pop('updated_at'))
            self.assertEqual({k: row[k] for k in old}, old)
            self.assertEqual(row['conversation_retention_days'], 30)
            definition = catalog_fingerprint(c)
        self.assertFalse(install(self.owner, settings))
        with connect(self.owner) as c:
            self.assertEqual(catalog_fingerprint(c), definition)

    def test_business_day_is_owned_by_user_turn_and_image_not_retained(self):
        self.ready(Settings(3, 'America/New_York', 4))
        moment = datetime.fromisoformat('2026-11-01T03:59:59-05:00')
        body = {'model': 'synthetic-model', 'messages': [
            {'role': 'user', 'content': '旧历史不重复存'},
            {'role': 'assistant', 'content': '旧回复'},
            {'role': 'user', 'content': [{'type': 'text', 'text': '看图'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,PRIVATE_IMAGE'}}]}]}
        record = self.store.begin(body, {}, now=moment)
        self.store.finish(record, content='合成回复', finish_reason='stop', usage={'total_tokens': 12}, now=moment+timedelta(seconds=1))
        with connect(self.app) as c:
            turn = c.execute('SELECT * FROM memory_core.conversation_turns').fetchone()
            self.assertEqual(turn['business_date'], '2026-10-31')
            self.assertTrue(turn['created_at'].endswith('-05:00'))
            rows = c.execute('SELECT * FROM memory_core.conversation_messages ORDER BY id').fetchall()
            self.assertEqual(len(rows), 2)
            self.assertNotIn('business_date', rows[0])
            self.assertEqual(rows[0]['content_parts'][1], {'type': 'image_placeholder'})
            self.assertNotIn('PRIVATE_IMAGE', str(rows))
            self.assertNotIn('旧历史', str(rows))
            self.assertEqual(turn['generation_state'], 'complete')

    def test_failure_partial_completion_idempotency_and_space_constraints(self):
        self.ready()
        record = self.store.begin(BODY, {'request_id': 'first'})
        other = ConversationStore(self.app, 'two').begin(BODY, {})
        with self.assertRaises(DuplicateRequest):
            self.store.begin(BODY, {'request_id': 'first'})
        with self.assertRaises(ValueError):
            self.store.begin(BODY, {'target_turn_id': other['turn_id']})
        self.store.finish(record, state='upstream_failed', content='不能保存的错误回复')
        self.assertFalse(self.store.finish(record, content='重复回复', finish_reason='stop'))
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.conversation_messages WHERE turn_id=%s', (record['turn_id'],)).fetchone()['n'], 1)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with connect(self.app) as c:
                c.execute('UPDATE memory_core.conversation_turns SET replaces_turn_id=%s WHERE id=%s', (other['turn_id'], record['turn_id']))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with connect(self.app) as c:
                c.execute("UPDATE memory_core.conversation_space_state SET active_turn_id=%s,lease_token=gen_random_uuid(),lease_expires_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id='one'", (other['turn_id'],))
        partial = self.store.begin(BODY, {})
        self.store.finish(partial, content='生成到一半', state='interrupted')
        with connect(self.app) as c:
            self.assertEqual(c.execute("SELECT content_state FROM memory_core.conversation_messages WHERE turn_id=%s AND role='assistant'", (partial['turn_id'],)).fetchone()['content_state'], 'incomplete')

    def test_concurrent_client_id_only_creates_one_user_message(self):
        self.ready()
        def submit(_):
            try:
                return self.store.begin(BODY, {'request_id': 'concurrent'})
            except DuplicateRequest:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_retention_clears_originals_copies_and_preserves_tombstones(self):
        self.ready()
        moment = datetime.fromisoformat('2026-10-01T02:59:00+08:00')
        record = self.store.begin(BODY, {}, now=moment)
        self.store.finish(record, content='合成回答', finish_reason='tool_calls', now=moment+timedelta(seconds=1), tools=[{'id': 'call1', 'function': {'name': 'synthetic', 'arguments': '{}'}}])
        with connect(self.app) as c:
            message = c.execute('SELECT id FROM memory_core.conversation_messages WHERE turn_id=%s ORDER BY id LIMIT 1', (record['turn_id'],)).fetchone()['id']
            segments = [{'segment_id': 'conversation-message:' + str(message), 'text': '原文副本'}]
            c.execute("INSERT INTO memory_core.source_materials(bot_id,source_key,content_sha256,business_date,source_kind,segments) VALUES ('one','conversation:test','synthetic','2026-09-30','mainline',%s)", (Jsonb(segments),))
        self.assertEqual(self.store.expire(now=moment+timedelta(days=29)), 0)
        self.assertEqual(self.store.expire(now=moment+timedelta(days=30)), 1)
        self.assertEqual(self.store.expire(now=moment+timedelta(days=31)), 0)
        with connect(self.app) as c:
            self.assertTrue(all(m['content_parts'] is None for m in c.execute('SELECT * FROM memory_core.conversation_messages')))
            self.assertIsNone(c.execute('SELECT payload FROM memory_core.conversation_tool_events').fetchone()['payload'])
            self.assertIsNone(c.execute('SELECT segments FROM memory_core.source_materials').fetchone()['segments'])
            self.assertIsNotNone(c.execute('SELECT content_deleted_at FROM memory_core.conversation_turns').fetchone()['content_deleted_at'])
        record = self.store.begin(BODY, {}, now=moment)
        config = SpaceConfiguration(self.owner)
        config.configure('one', Participant('用户'), Participant('助手'), expected_revision=1,
                         conversation_retention_days=None)
        config.configure('one', Participant('用户'), Participant('助手'), expected_revision=2)
        self.assertEqual(self.store.expire(now=moment+timedelta(days=365)), 0)

    def test_late_response_cannot_restore_expired_originals(self):
        self.ready()
        moment = datetime.fromisoformat('2026-09-01T12:00:00+08:00')
        record = self.store.begin(BODY, {}, now=moment)
        self.store.expire(now=moment+timedelta(days=30))
        self.store.finish(record, content='迟到的回复', finish_reason='stop')
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.conversation_messages').fetchone()['n'], 1)
            self.assertEqual(c.execute('SELECT error_code FROM memory_core.conversation_requests').fetchone()['error_code'], 'source_expired')
