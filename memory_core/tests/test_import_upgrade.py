"""Import schema upgrades using the real installer and application role."""

from datetime import datetime, timezone
import hashlib
import json

import psycopg

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.cards.drafts import DraftStore, context_for
from memory_core.storage.install import install, catalog_fingerprint
from memory_core.imports.imports import ImportStore
from memory_core.imports.import_formats import ImportFormatError, parse_upload
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_deepseek_import import deepseek_zip
from memory_core.tests.test_imports import SyntheticStructurer
from memory_core.tests.test_kelivo_import import kelivo_backup
from memory_core.tests.test_operit_import import operit_zip


class ImportUpgradeTests(DatabaseTestCase):
    def test_v32_upgrade_recovers_stale_drafts_extractions_and_import_retry(self):
        settings = Settings(3)
        install(self.owner, settings, target_version=32)
        SpaceConfiguration(self.owner).configure('one', Participant('合成读者'), Participant('合成助手'))
        store = ImportStore(self.app, 'one')
        now = datetime.now(timezone.utc)
        materials = parse_upload('memory', [('sample.md', '## 2025-03-14\n合成失败材料。'.encode())], settings)
        job = store.create('memory', materials, now=now)
        store.confirm(job['id'], now=now)
        item = store.prepare_next(job['id'], now=now)
        topic = store.fallback_topic()['id']
        model = SyntheticStructurer(topic)
        model.fail = True
        extraction = store.extract(item['extraction_id'], selector=None, structurer=model)
        failed = extraction['drafts'][0]
        store.finish_item(item, now=now, error='import_structure_failed')

        model.fail = False
        ready_id = store.submit_memory('ready', 'ready', '合成待审材料。', '2025-03-15')
        ready_extraction = store.extract(ready_id, selector=None, structurer=model)
        ready = ready_extraction['drafts'][0]
        rejected_id = store.submit_memory('rejected', 'rejected', '合成拒绝材料。', '2025-03-16')
        rejected = store.extraction(rejected_id)['drafts'][0]
        rejected = store.reject(rejected['id'], expected_version=rejected['version'])

        with connect(self.owner) as c:
            context = context_for(c, 'one')
            legacy_input = {k: v for k, v in context.items() if k not in ('policy_revision', 'known_entities')}
            old_digest = hashlib.sha256(json.dumps(legacy_input, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            c.execute('UPDATE memory_core.import_jobs SET context_digest=%s WHERE id=%s', (old_digest, job['id']))
            c.execute("""INSERT INTO memory_core.topic_summary_batches
                (bot_id,topic_id,cycle_date,window_start,window_end,request_key,policy_revision,context_digest,status)
                VALUES ('one',%s,'2025-04-03','2025-03','2025-03',%s,1,%s,'failed'),
                       ('one',%s,'2025-04-03','2025-03','2025-03',%s,1,%s,'rejected')""",
                (topic, 'a'*64, 'b'*64, topic, 'c'*64, 'b'*64))
            # Emulate a save by the old application, which left unfinished rows behind.
            for core_memory in ('新核心记忆。', '更新核心记忆。', '再次更新核心记忆。'):
                c.execute("UPDATE memory_core.space_policies SET core_memory=%s,revision=revision+1 WHERE bot_id='one'",
                          (core_memory,))

        self.assertTrue(install(self.owner, settings))
        self.assertFalse(install(self.owner, settings))
        upgraded = ImportStore(self.app, 'one')
        drafts = DraftStore(self.app, 'one')
        repaired = drafts.get(failed['id'])
        self.assertEqual(repaired, {**failed, 'policy_revision': 4, 'version': failed['version'] + 1})
        self.assertEqual(drafts.get(ready['id']), {**ready, 'policy_revision': 4, 'version': ready['version'] + 1})
        self.assertEqual(drafts.get(rejected['id']), rejected)
        self.assertEqual(upgraded.extraction(ready_id)['policy_revision'], 1)
        retry = upgraded.extraction(extraction['id'])
        self.assertEqual((retry['policy_revision'], retry['version']), (4, extraction['version'] + 1))
        with connect(self.app) as c:
            batches = c.execute("SELECT status,policy_revision,version FROM memory_core.topic_summary_batches ORDER BY id").fetchall()
        self.assertEqual(batches, [dict(status='failed', policy_revision=4, version=2),
            dict(status='rejected', policy_revision=1, version=1)])
        self.assertEqual(upgraded.structure_retry_target(failed['id'], expected_version=repaired['version']), repaired)
        self.assertEqual(upgraded.retry(job['id'], now=now)['status'], 'queued')
        item = upgraded.prepare_next(job['id'], now=now)
        upgraded.resume(item)
        result = upgraded.extract(item['extraction_id'], selector=None, structurer=model)
        self.assertEqual(result['drafts'][0]['status'], 'pending')
        upgraded.finish_item(item, now=now)
        self.assertEqual(upgraded.get(job['id'])['progress']['processed'], 1)

    def test_existing_import_keeps_review_mode_after_upgrade(self):
        settings=Settings(3)
        install(self.owner,settings,target_version=22)
        SpaceConfiguration(self.owner).configure('one',Participant('合成读者'),Participant('合成助手'))
        store=ImportStore(self.app,'one')
        content='## 2025-03-14\n合成原材料。'.encode()
        now=datetime.now(timezone.utc)
        job=store.create('memory',parse_upload('memory',[('sample.md',content)],settings),now=now)
        store.confirm(job['id'],now=now)
        install(self.owner,settings)
        upgraded=ImportStore(self.app,'one')
        self.assertFalse(upgraded.get(job['id'])['auto_approve'])
        self.assertFalse(upgraded.prepare_next(job['id'],now=now)['auto_approve'])
        estimate = 'estimated_remaining_seconds'
        self.assertNotIn(estimate, upgraded.get(job['id'],now=now)['progress'])
        upgraded.stop(job['id'],now=now)
        upgraded.retry(job['id'],now=now)
        upgraded.recover_running(job['id'],now=now)
        upgraded.prepare_next(job['id'],now=now)
        self.assertIsNone(upgraded.get(job['id'],now=now)['progress'][estimate])

    def test_v21_snapshot_data_survives_v22_upgrade_and_repeat(self):
        install(self.owner,Settings(3),target_version=21)
        config=SpaceConfiguration(self.owner)
        config.configure('one',Participant('合成读者'),Participant('合成助手'))
        store=DraftStore(self.app,'one')
        extraction=store.submit('synthetic-existing','synthetic-request',[
            dict(segment_id='m-0',speaker='user',text='合成已有原材料。',timestamp=None)],business_date='2025-03-14')
        with connect(self.owner) as c:
            columns=c.execute("SELECT column_name,data_type,is_nullable FROM information_schema.columns WHERE table_schema='memory_core' AND table_name='source_materials'").fetchall()
            self.assertIn(dict(column_name='segments',data_type='jsonb',is_nullable='YES'),columns)
        install(self.owner,Settings(3))
        with connect(self.app) as c:
            row=c.execute('SELECT material_kind,segments FROM memory_core.source_materials WHERE id=%s',
                (store.extraction(extraction)['material_id'],)).fetchone()
            self.assertEqual(row['material_kind'],'chat')
            self.assertEqual(row['segments'][0]['text'],'合成已有原材料。')
        with connect(self.owner) as c:
            before=catalog_fingerprint(c)
        self.assertFalse(install(self.owner,Settings(3)))
        with connect(self.owner) as c:
            self.assertEqual(catalog_fingerprint(c),before)

    def test_existing_import_survives_upgrade_that_enables_kelivo(self):
        settings = Settings(3)
        install(self.owner, settings, target_version=24)
        SpaceConfiguration(self.owner).configure('one', Participant('合成读者'), Participant('合成助手'))
        store = ImportStore(self.app, 'one')
        now = datetime.now(timezone.utc)
        old = store.create('memory', parse_upload('memory', [
            ('sample.md', '## 2025-03-14\n合成旧材料。'.encode()),
        ], settings), now=now)
        items = parse_upload('kelivo', [('synthetic.zip', kelivo_backup())], settings)
        with self.assertRaisesRegex(ImportFormatError, '版本 25'):
            store.create('kelivo', items, now=now)

        self.assertTrue(install(self.owner, settings))
        upgraded = ImportStore(self.app, 'one')
        self.assertEqual(upgraded.get(old['id'])['source'], 'memory')
        new = upgraded.create('kelivo', items, now=now)
        self.assertEqual(upgraded.preview(new['id'], date_end='2025-03-13')['material_count'], 1)

    def test_v26_upgrade_enables_deepseek_and_preserves_existing_jobs(self):
        settings = Settings(3)
        install(self.owner, settings, target_version=26)
        SpaceConfiguration(self.owner).configure('one', Participant('合成读者'), Participant('合成助手'))
        store = ImportStore(self.app, 'one')
        now = datetime.now(timezone.utc)
        old = store.create('kelivo', parse_upload('kelivo', [('synthetic.zip', kelivo_backup())], settings), now=now)
        items = parse_upload('deepseek', [('synthetic.zip', deepseek_zip())], settings)
        with self.assertRaisesRegex(ImportFormatError, '版本 27'):
            store.create('deepseek', items, now=now)
        self.assertTrue(install(self.owner, settings))
        upgraded = ImportStore(self.app, 'one')
        self.assertEqual(upgraded.get(old['id'])['source'], 'kelivo')
        new = upgraded.create('deepseek', items, now=now)
        self.assertEqual(upgraded.preview(new['id'])['material_count'], 2)
        self.assertFalse(install(self.owner, settings))

    def test_v27_preview_can_be_discarded_after_upgrade_without_direct_delete_grants(self):
        settings = Settings(3)
        install(self.owner, settings, target_version=27)
        SpaceConfiguration(self.owner).configure('one', Participant('合成读者'), Participant('合成助手'))
        store = ImportStore(self.app, 'one')
        items = parse_upload('memory', [('sample.md', '## 2025-03-14\n合成待确认材料。'.encode())], settings)
        job = store.create('memory', items, now=datetime.now(timezone.utc))
        with self.assertRaisesRegex(ValueError, '版本 28'):
            store.discard(job['id'])
        self.assertTrue(install(self.owner, settings))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with connect(self.app) as c:
                c.execute('DELETE FROM memory_core.import_items WHERE job_id=%s', (job['id'],))
        upgraded = ImportStore(self.app, 'one')
        self.assertEqual(upgraded.preview(job['id'])['material_count'], 1)
        upgraded.discard(job['id'])
        self.assertEqual(upgraded.list_jobs(), [])
        self.assertFalse(install(self.owner, settings))

    def test_v31_queued_kelivo_memory_still_runs_after_operit_upgrade(self):
        settings = Settings(3)
        install(self.owner, settings, target_version=31)
        SpaceConfiguration(self.owner).configure('one', Participant('合成读者'), Participant('合成助手'))
        store = ImportStore(self.app, 'one')
        now = datetime.now(timezone.utc)
        operit = parse_upload('operit', [('synthetic.zip', operit_zip())], settings)
        with self.assertRaisesRegex(ImportFormatError, '版本 32'):
            store.create('operit', operit, now=now)
        backup = kelivo_backup(multiple_assistants=True, with_memories=True)
        items = parse_upload('kelivo', [('synthetic.zip', backup)], settings)
        job = store.create('kelivo', items, now=now)
        undated = next(item for item in items if item.is_memory_entry and item.business_date is None)
        store.confirm(job['id'], now=now, selected_conversations=[], selected_memories=[undated.source_key])

        self.assertTrue(install(self.owner, settings))
        upgraded = ImportStore(self.app, 'one')
        item = upgraded.prepare_next(job['id'], now=now)
        self.assertEqual(item['source_key'], undated.source_key)
        self.assertTrue(item['is_memory_entry'])
        with connect(self.app) as connection:
            material = connection.execute('SELECT material_kind,business_date FROM memory_core.source_materials WHERE source_key=%s',
                                          (undated.source_key,)).fetchone()
        self.assertEqual(material, dict(material_kind='memory', business_date=None))
        new = upgraded.create('operit', operit, now=now)
        self.assertEqual(len(upgraded.preview(new['id'])['assistant_groups']), 2)
        self.assertFalse(install(self.owner, settings))
