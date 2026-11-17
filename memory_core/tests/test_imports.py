"""Real isolated Postgres, official-shaped synthetic uploads and synthetic models."""

from datetime import datetime, timedelta, timezone
import json

import psycopg

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.import_formats import parse_upload
from memory_core.imports import ImportStore, import_run_lock
from memory_core.install import install
from memory_core.review import ReviewStore
from memory_core.space import Participant, RevisionConflict, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_import_formats import claude, uploaded


class SyntheticSelector:
    def __init__(self):
        self.calls=0

    def extract(self,prompt):
        self.calls+=1
        material=json.loads(prompt.split('来源材料 JSON：\n')[-1]) if '来源材料 JSON：\n' in prompt else json.loads(prompt.rsplit('\n',2)[1])
        segment=material['segments'][0]
        content=segment['text']
        return json.dumps(dict(memories=[dict(title='合成标题',content=content,summary=content,importance=8,
            evidence=[dict(segment_id=segment['segment_id'],quote=content)])],skipped_reason=None))


class SyntheticStructurer:
    def __init__(self, topic):
        self.topic=topic
        self.calls=0
        self.fail=False
        self.rewrite=None

    def extract(self,prompt):
        self.calls+=1
        if self.fail:
            return 'invalid synthetic output'
        material=json.loads(prompt.rsplit('\n',2)[1])
        memory='title' not in material
        result=dict(summary='合成检索摘要。',experience_mode='external',cognitive_status='stable',
            decay_layer='tier_4_episode',source_date=material.get('source_date',material.get('business_date')),
            is_milestone=False,valence=None,arousal=None,entities=[],relations=[],topic_ids=[self.topic])
        if memory:
            result['title']='合成记忆标题'
            result['content']=self.rewrite
        return json.dumps(result)


class ImportStoreTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.core=Settings(3)
        install(self.owner,self.core)
        self.config=SpaceConfiguration(self.owner)
        self.config.configure('one',Participant('合成读者'),Participant('合成助手'))
        self.config.configure('two',Participant('另一读者'),Participant('另一助手'))
        self.store=ImportStore(self.app,'one')
        self.selector=SyntheticSelector()
        self.structurer=SyntheticStructurer(self.store.fallback_topic()['id'])
        self.now=datetime.now(timezone.utc)

    def upload(self, source='claude', files=None):
        files=files or uploaded([claude()])
        items=parse_upload(source,files,self.core)
        return self.store.create(source,items,now=self.now)

    def process(self, identifier):
        while self.store.get(identifier)['status'] in ('queued','running'):
            item=self.store.prepare_next(identifier,now=self.now)
            if item is None or item.get('skip'):
                continue
            self.store.resume(item)
            result=self.store.extract(item['extraction_id'],selector=self.selector,structurer=self.structurer,clock=lambda:self.now)
            self.store.finish_item(item,now=self.now,error=None if result['status']=='complete' else 'import_structure_failed')

    def test_preview_confirm_extract_and_existing_review_share_one_pipeline(self):
        job=self.upload()
        self.assertEqual(job['status'],'preview')
        self.assertEqual(self.selector.calls,0)
        plan=self.store.preview(job['id'])
        self.assertEqual((plan['day_count'],plan['message_count']),(1,2))
        self.store.confirm(job['id'],now=self.now)
        self.process(job['id'])
        done=self.store.get(job['id'])
        self.assertEqual((done['status'],done['progress']['processed']),('completed',1))
        draft=self.store.list()[0]
        self.assertEqual(draft['status'],'pending')
        with connect(self.app) as c:
            item=c.execute('SELECT * FROM memory_core.import_items WHERE job_id=%s',(job['id'],)).fetchone()
            self.assertIsNone(item['segments'])
            for table in ('conversation_turns','conversation_messages','conversation_summaries','conversation_diaries','daily_tasks'):
                self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.'+table).fetchone()['n'],0,table)
        review=ReviewStore(self.app,'one')
        class Embedder:
            def embed(self,text): return [1,0,0]
        result=review.approve(draft['id'],expected_version=draft['version'],
            expected_policy_revision=draft['policy_revision'],request_key='synthetic-import-review',
            embedder=Embedder(),embedding_model='synthetic-vector')
        self.assertEqual(result['status'],'approved')

    def test_changed_content_same_conversation_and_date_is_skipped_even_after_source_deleted(self):
        first=self.upload()
        self.store.confirm(first['id'],now=self.now)
        self.process(first['id'])
        extraction=self.store.list()[0]['extraction_id']
        self.store.delete_material(self.store.extraction(extraction)['material_id'])
        changed=claude()
        changed['chat_messages'][0]['text']='合成新正文，不改变来源日期。'
        second=self.upload(files=uploaded([changed]))
        plan=self.store.preview(second['id'])
        self.assertEqual((plan['skipped_count'],plan['model_calls_max']),(1,0))
        done=self.store.confirm(second['id'],now=self.now)
        self.assertEqual(done['status'],'completed')
        self.assertEqual((self.selector.calls,self.structurer.calls),(1,1))

    def test_duplicate_uploaded_before_other_job_finishes_is_checked_again(self):
        first,second=self.upload(),self.upload()
        for job in (first,second):
            self.store.confirm(job['id'],now=self.now)
        self.process(first['id'])
        self.process(second['id'])
        self.assertEqual(self.store.get(second['id'])['progress']['skipped'],1)
        self.assertEqual(self.selector.calls,1)

    def test_memory_bypasses_selection_and_quota_preserves_body_and_date(self):
        original='合成原文：每周练习陶艺。\n\n第二段保持原样。'
        data=('## 2025-03-14\n'+original+'\n## 2025-03-14\n合成第二条记忆。').encode()
        job=self.upload('memory',[('memory.md',data)])
        self.store.confirm(job['id'],now=self.now)
        self.process(job['id'])
        drafts=self.store.list()
        self.assertEqual(len(drafts),2)
        self.assertEqual(self.selector.calls,0)
        self.assertIn(original,[draft['payload']['content'] for draft in drafts])
        self.assertEqual(drafts[0]['payload']['title'],'合成记忆标题')
        self.assertEqual(drafts[0]['payload']['source_date'],'2025-03-14')
        with connect(self.app) as c:
            self.assertEqual(self.store.daily_candidate_count(c,'2025-03-14'),0)
        class Embedder:
            def embed(self,text): return [1,0,0]
        draft=drafts[0]
        result=ReviewStore(self.app,'one').approve(draft['id'],expected_version=draft['version'],
            expected_policy_revision=draft['policy_revision'],request_key='synthetic-memory-import-review',
            embedder=Embedder(),embedding_model='synthetic-vector')
        self.assertEqual(result['status'],'approved')
        with connect(self.app) as c:
            card=c.execute("SELECT * FROM memory_core.memory_items WHERE bot_id='one'").fetchone()
        self.assertEqual(card['source_date'],'2025-03-14')
        self.assertEqual(datetime.fromisoformat(card['created_at']).date(),self.now.astimezone(datetime.fromisoformat(card['created_at']).tzinfo).date())

    def test_memory_rewrite_keeps_original_and_exact_evidence(self):
        original='我今天跟合成助手说：“我决定练陶艺。”'
        self.structurer.rewrite='合成读者向合成助手说：“我决定练陶艺。”'
        job=self.upload('memory',[('memory.md',f'## 2025-03-14\n{original}'.encode())])
        self.store.confirm(job['id'],now=self.now)
        self.process(job['id'])
        draft=self.store.list()[0]
        self.assertEqual(draft['payload']['content'],self.structurer.rewrite)
        self.assertEqual(draft['payload']['evidence'],[dict(segment_id='m-0',quote=original)])
        self.assertEqual(self.store.source(draft['id'])['segments'][0]['text'],original)
        self.assertEqual(self.selector.calls,0)

    def test_restart_resumes_saved_extraction_and_failed_structure_only(self):
        job=self.upload()
        self.store.confirm(job['id'],now=self.now)
        item=self.store.prepare_next(job['id'],now=self.now)
        self.structurer.fail=True
        result=self.store.extract(item['extraction_id'],selector=self.selector,structurer=self.structurer)
        self.assertEqual(result['status'],'selected')
        self.assertEqual(self.selector.calls,1)
        # Simulate loss of the process after DraftStore saved candidates but
        # before the queue item was settled: both remain durable and running.
        self.store=ImportStore(self.app,'one')
        self.store.recover_running(job['id'],now=self.now)
        self.structurer.fail=False
        self.process(job['id'])
        self.assertEqual(self.selector.calls,1)
        self.assertEqual(len(self.store.list()),1)
        self.assertEqual(self.store.get(job['id'])['status'],'completed')

    def test_scope_and_foreign_keys_block_cross_space_progress(self):
        job=self.upload()
        with self.assertRaises(ValueError):
            ImportStore(self.app,'two').get(job['id'])
        with self.assertRaises(psycopg.errors.ForeignKeyViolation),connect(self.app) as c:
            c.execute("UPDATE memory_core.import_items SET bot_id='two' WHERE job_id=%s",(job['id'],))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege),connect(self.app) as c:
            c.execute('DELETE FROM memory_core.import_jobs WHERE id=%s',(job['id'],))

    def test_session_lock_is_exclusive_and_released_for_restart(self):
        with import_run_lock(self.app) as first:
            self.assertIsNotNone(first)
            with import_run_lock(self.app) as second:
                self.assertIsNone(second)
        with import_run_lock(self.app) as recovered:
            self.assertIsNotNone(recovered)

    def test_configuration_change_pauses_without_calling_models_then_can_resume(self):
        job=self.upload()
        self.store.confirm(job['id'],now=self.now)
        self.config.set_core_memory('one',expected_revision=1,core_memory='合成的设置变动。')
        with self.assertRaises(RevisionConflict):
            self.store.prepare_next(job['id'],now=self.now)
        self.store.fail_job(job['id'],'import_configuration_changed',now=self.now)
        with self.assertRaises(RevisionConflict):
            self.store.retry(job['id'],now=self.now)
        self.assertEqual(self.selector.calls,0)
        self.config.set_core_memory('one',expected_revision=2,core_memory='')
        self.store.retry(job['id'],now=self.now)
        self.process(job['id'])
        self.assertEqual(self.store.get(job['id'])['status'],'completed')
        self.assertEqual(self.selector.calls,1)

    def test_create_consumes_material_generator_once_and_rolls_back_late_errors(self):
        items=parse_upload('claude',uploaded([claude(key='synthetic-'+str(n)) for n in range(130)]),self.core)
        job=self.store.create('claude',iter(items),now=self.now)
        self.assertEqual(job['progress']['total'],130)
        plan=self.store.preview(job['id'])
        self.assertEqual(plan['message_count'],260)
        with connect(self.app) as c:
            self.assertNotIn('segments',self.store._items(c,job)[0])
        def broken():
            yield from items
            raise ValueError('synthetic late parsing error')
        with self.assertRaisesRegex(ValueError,'late parsing'):
            self.store.create('claude',broken(),now=self.now)
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.import_jobs').fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.import_items').fetchone()['n'],130)

    def test_retention_removes_preview_and_source_but_keeps_dedup_key(self):
        with connect(self.owner) as c:
            c.execute("UPDATE memory_core.space_policies SET source_retention_days=30 WHERE bot_id='one'")
        preview=self.upload()
        job=self.upload('memory',[('memory.md','## 2025-03-14\n合成记忆。'.encode())])
        self.store.confirm(job['id'],now=self.now)
        self.process(job['id'])
        self.assertGreater(self.store.expire(now=self.now+timedelta(days=1000)),0)
        self.assertEqual(self.store.get(preview['id'])['error_code'],'import_material_expired')
        with connect(self.app) as c:
            source=c.execute("SELECT * FROM memory_core.source_materials WHERE material_kind='memory'").fetchone()
            self.assertIsNone(source['segments'])
            self.assertIsNotNone(source['deleted_at'])
            self.assertTrue(source['source_key'])
