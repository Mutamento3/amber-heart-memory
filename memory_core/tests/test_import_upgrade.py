"""Import schema upgrades using the real installer and application role."""

from datetime import datetime, timezone

import psycopg

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.cards.drafts import DraftStore
from memory_core.storage.install import install, catalog_fingerprint
from memory_core.imports.imports import ImportStore
from memory_core.imports.import_formats import ImportFormatError, parse_upload
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_deepseek_import import deepseek_zip
from memory_core.tests.test_kelivo_import import kelivo_backup


class ImportUpgradeTests(DatabaseTestCase):
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
