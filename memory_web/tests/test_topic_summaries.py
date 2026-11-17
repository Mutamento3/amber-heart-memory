import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from memory_core.config import Settings
from memory_core.daily_tasks import DailyTasks
from memory_core.db import connect
from memory_core.install import install
from memory_core.model_config import ModelRole, RecallModelsConfig
from memory_core.postgres import PostgresStore
from memory_core.space import Participant, SpaceConfiguration
from memory_core.testing.provider_server import ProviderServer
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_topic_summaries import NOW, Embeddings, response
from memory_core.topic_summaries import TopicSummaryStore
from memory_core.topic_summary_policy import TopicSummarySettings
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.daily_jobs import DailyJobs
from memory_web.install import install_web
from memory_web.password import write_password
from memory_web.testing.diary_model import SyntheticDiaryModel
from memory_web.topic_summary_model import TopicSummaryModel

ORIGIN='https://memory.example.test'


class FakeTopics:
    configured=True
    def __init__(self):
        self.calls=[]
        self.bad_window=None
        self.empty=False
    def generate(self,prompt,record):
        self.calls.append(prompt)
        record(dict(stage='topic_summary',model='synthetic-summary',state='succeeded',usage=dict(total_tokens=20)))
        if self.bad_window and f'本次材料的时间窗口：{self.bad_window}' in prompt:
            return '{invalid}'
        ids=[int(mid) for mid in re.findall(r'^\[(\d+)\]',prompt,re.M)]
        return json.dumps(response([dict(id=mid) for mid in ids],[] if self.empty else None),ensure_ascii=False)


class TopicWebTests(DatabaseTestCase):
    def setUp(self):
        super().setUp();self.addCleanup(super().tearDown)
        self.now=NOW
        self.core=Settings(3)
        install(self.owner,self.core);install_web(self.owner)
        self.config=SpaceConfiguration(self.owner)
        for space in ('one','two'):self.config.configure(space,Participant('合成读者'),Participant('合成助手'))
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        password=Path(folder.name)/'password.hash';write_password(password,'synthetic password')
        fixed=Path(folder.name)/'fixed.json';fixed.write_text(json.dumps(dict(embed={'合成读者在十月学习植物，并和合成助手一起整理观察笔记。':[1,0,0]})))
        self.settings=WebSettings(self.app,self.owner,ORIGIN,password,fixed_models_file=fixed)
        self.model=FakeTopics();self.embedder=Embeddings()
        self.store=TopicSummaryStore(self.app,'one')
        self.topic=next(t['id'] for t in self.store.topics() if t['preset_key']=='interests')
    def tearDown(self):pass
    def seed(self,dates=('2026-10-01',)*5):
        ids=[]
        for day in dates:
            mid=PostgresStore(self.app,'one').write_card('合成读者记录了植物学习的过程。',[1,0,0],title='合成植物笔记',source_date=day)
            with connect(self.app) as c:
                c.execute('UPDATE memory_core.memory_items SET primary_topic_id=%s,created_at=%s WHERE id=%s',(self.topic,NOW.isoformat(),mid))
                c.execute("INSERT INTO memory_core.memory_topics(bot_id,memory_id,topic_id) VALUES ('one',%s,%s)",(mid,self.topic))
            ids.append(mid)
        return ids
    def jobs(self,model=None,settings=None):
        return DailyJobs(settings or self.settings,self.core,model=SyntheticDiaryModel(),topic_summary_models=model or self.model,
            clock=lambda:self.now,embedder=self.embedder)
    def tick(self,jobs):
        async def run():await jobs.tick();await jobs.drain()
        asyncio.run(run())
    def batch(self):
        with connect(self.app) as c:return c.execute("SELECT * FROM memory_core.topic_summary_batches WHERE bot_id='one' ORDER BY id DESC LIMIT 1").fetchone()
    def client(self):
        application=create_app(self.settings,diary_models=SyntheticDiaryModel(),topic_summary_models=self.model,daily_clock=lambda:self.now)
        http=TestClient(application,base_url=ORIGIN);self.addCleanup(http.close)
        login=http.post('/api/login',headers={'Origin':ORIGIN},json={'password':'synthetic password'})
        return http,{'Origin':ORIGIN,'X-CSRF-Token':login.json()['csrf']}
    def test_monthly_closed_windows_and_default_path_unconfigured(self):
        self.seed();unconfigured=DailyJobs(self.settings,self.core,clock=lambda:self.now)
        self.assertFalse(unconfigured.topic_summary_models.configured)
        self.assertNotIn(('topic_summaries','2026-11-03'),unconfigured.work('one',self.now))
        jobs=self.jobs();self.tick(jobs);self.tick(jobs)
        self.assertEqual(len(self.model.calls),1)
        self.assertEqual(self.batch()['status'],'pending')
        with connect(self.app) as c:
            task=c.execute("SELECT * FROM memory_core.daily_tasks WHERE bot_id='one' AND task_kind='topic_summaries'").fetchone()
        self.assertEqual(task['state'],'done');self.assertEqual(len(task['model_usage']),1)
        http,_=self.client();overview=http.get('/api/spaces/one/overview').json()
        self.assertEqual(overview['pending'],1);self.assertEqual(overview['auto_approval']['ready_count'],1)
        self.assertEqual(overview['topic_summaries']['status'],'done')
        self.assertIn('2026-10',self.model.calls[0]);self.assertNotIn('主题总结未配置',self.model.calls[0])
    def test_bad_window_retries_format_once_then_other_windows_continue(self):
        self.seed(('2026-09-01',)*5 + ('2026-10-01',)*5)
        self.model.bad_window='2026-09'
        self.tick(self.jobs())
        self.assertEqual(len(self.model.calls),3)
        with connect(self.app) as c:
            batches = c.execute("SELECT window_start,status,error_code FROM memory_core.topic_summary_batches ORDER BY window_start").fetchall()
            task = c.execute("SELECT state,result FROM memory_core.daily_tasks WHERE task_kind='topic_summaries'").fetchone()
        self.assertEqual([(b['window_start'],b['status']) for b in batches],[('2026-09','failed'),('2026-10','pending')])
        self.assertTrue(batches[0]['error_code'])
        self.assertEqual(task['state'],'done')
        self.assertEqual(task['result']['failed_windows'],1)
        self.now=NOW+timedelta(days=1)
        self.tick(self.jobs())
        self.assertEqual(len(self.model.calls),3)
    def test_shared_48_hour_rule_automatic_apply_and_no_duplicate(self):
        ids=self.seed();self.tick(self.jobs());batch=self.batch()
        self.now=NOW+timedelta(days=1);self.tick(self.jobs());self.assertEqual(self.batch()['status'],'pending')
        # Counted in business days: made on day D, applied in the run for D + 2.
        self.now=NOW+timedelta(days=2);self.tick(self.jobs());self.assertEqual(self.batch()['status'],'approved')
        parent=self.batch()['summary_card_id'];self.tick(self.jobs())
        self.assertEqual(self.batch()['summary_card_id'],parent);self.assertEqual(len(self.embedder.calls),1)
        with connect(self.app) as c:
            rows=c.execute('SELECT parent_reflection_id FROM memory_core.memory_items WHERE id=ANY(%s) ORDER BY id',(ids,)).fetchall()
        self.assertEqual([r['parent_reflection_id'] for r in rows],[parent,None,None,None,None])
    def test_switch_off_prevents_summary_auto_approval(self):
        self.seed();self.tick(self.jobs());self.config.set_auto_approval('one',expected_revision=1,enabled=False)
        self.now=NOW+timedelta(days=3);self.tick(self.jobs());self.assertEqual(self.batch()['status'],'pending');self.assertEqual(self.embedder.calls,[])
    def test_api_readonly_auth_csrf_cross_space_approve_revoke(self):
        self.seed();self.tick(self.jobs());batch=self.batch();http,headers=self.client();base=f"/api/spaces/one/topic-summaries/{batch['id']}"
        listing=http.get('/api/spaces/one/drafts').json();self.assertEqual(listing['items'][0]['item_kind'],'topic_summary')
        detail=http.get(base).json();self.assertEqual(len(detail['sources']),5)
        self.assertEqual(http.put(base,headers=headers,json={}).status_code,405)
        self.assertEqual(http.get(base.replace('/one/','/two/')).status_code,400)
        payload=dict(expected_version=batch['version'],expected_policy_revision=1,request_key='synthetic-approve')
        self.assertEqual(http.post(base+'/approve',headers={'Origin':ORIGIN},json=payload).status_code,403)
        stale=http.post(base+'/approve',headers=headers,json=payload|dict(expected_version=999));self.assertEqual(stale.status_code,409)
        result=http.post(base+'/approve',headers=headers,json=payload);self.assertEqual(result.status_code,200,result.text)
        parent=result.json()['summary_card_id'];self.assertEqual(http.post(base+'/approve',headers=headers,json=payload).json()['summary_card_id'],parent)
        card=http.get(f'/api/spaces/one/cards/{parent}').json();self.assertEqual(card['topic_summary']['id'],batch['id'])
        rejected_edit=http.patch(f'/api/spaces/one/cards/{parent}',headers=headers,json=dict(title='不应改动',topic_ids=[self.topic],is_milestone=False))
        self.assertEqual(rejected_edit.status_code,400)
        result=http.post(base+'/revoke',headers=headers,json=dict(expected_version=result.json()['version']));self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(http.get(f'/api/spaces/one/cards/{parent}').status_code,400)
        http.cookies.clear();self.assertEqual(http.get(base).status_code,401)
    def test_reject_delays_until_next_larger_window(self):
        self.seed();self.tick(self.jobs());batch=self.batch();http,headers=self.client()
        result=http.post(f"/api/spaces/one/topic-summaries/{batch['id']}/reject",headers=headers,json=dict(expected_version=batch['version']))
        self.assertEqual(result.status_code,200,result.text)
        self.now=NOW+timedelta(days=1);self.tick(self.jobs());self.assertEqual(len(self.model.calls),1)
        self.now=NOW.replace(month=12);self.tick(self.jobs());self.assertEqual(len(self.model.calls),2)
        self.assertEqual(self.batch()['window_end'],'2026-11')
    def test_concurrent_daily_workers_and_existing_failed_retry_use_one_batch(self):
        self.seed()
        async def run():
            first,second=self.jobs(),self.jobs()
            await asyncio.gather(first.tick(),second.tick());await asyncio.gather(first.drain(),second.drain())
        asyncio.run(run());self.assertEqual(len(self.model.calls),1)
    def test_skipped_all_and_small_month_do_not_create_empty_review(self):
        self.seed(('2026-09-01',)*2);self.tick(self.jobs());self.assertIsNone(self.batch())
        self.seed(('2026-10-01',)*3);self.model.empty=True
        # A completed monthly sweep is not rerun until next month.
        self.now=NOW.replace(month=12);self.tick(self.jobs());self.assertEqual(self.batch()['status'],'empty')
    def test_default_real_adapter_targets_only_fake_http_provider(self):
        ids=self.seed()
        previous=PostgresStore(self.app,'one').write_card('上月已写过植物课程的起因。',[1,0,0],
            title='上月已生效的合成总结',item_kind='topic_summary')
        with connect(self.app) as c:
            c.execute('UPDATE memory_core.memory_items SET primary_topic_id=%s WHERE id=%s',(self.topic,previous))
        with ProviderServer() as server:
            server.replies['synthetic-topic-role']=json.dumps(response([dict(id=i) for i in ids]))
            settings=replace(self.settings,topic_summary_model='synthetic-topic-role',topic_summary_base_url=server.url,topic_summary_api_key='synthetic-topic-key')
            jobs=DailyJobs(settings,self.core,model=SyntheticDiaryModel(),clock=lambda:self.now,embedder=self.embedder)
            self.tick(jobs)
            calls=[r for r in server.requests if r['body'].get('model')=='synthetic-topic-role']
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['authorization'],'Bearer synthetic-topic-key')
        self.assertEqual((calls[0]['body']['temperature'], calls[0]['body']['max_tokens']), (0, 12000))
        prompt=calls[0]['body']['messages'][0]['content']
        self.assertIn('80–350 字，这是硬限制', prompt)
        self.assertIn('该主题已有的总结（仅作避免重复的背景，不是本窗口来源）',prompt)
        self.assertIn('上月已生效的合成总结：上月已写过植物课程的起因。',prompt)
        with connect(self.app) as c:
            sources=c.execute('SELECT memory_id FROM memory_core.topic_summary_sources WHERE batch_id=%s ORDER BY ordinal',(self.batch()['id'],)).fetchall()
        self.assertEqual([r['memory_id'] for r in sources],ids)
        self.assertEqual(self.batch()['status'],'pending');self.assertNotIn('synthetic-topic-key',str(self.batch()))
    def test_model_role_validation_and_fake_transport_usage_redaction(self):
        settings=replace(self.settings,topic_summary_model='synthetic-role',topic_summary_base_url='https://model.example.test/v1',topic_summary_api_key='synthetic-secret')
        self.assertNotIn('synthetic-secret',repr(settings))
        with self.assertRaises(ValueError):replace(settings,topic_summary_base_url=None)
        records=[]
        def handler(request):
            self.assertEqual(json.loads(request.content)['model'],'synthetic-role')
            return httpx.Response(200,json=dict(choices=[dict(message=dict(content='{}'),finish_reason='stop')],usage=dict(total_tokens=8,body='DO_NOT_SAVE')))
        model=TopicSummaryModel(settings,transport=httpx.MockTransport(handler))
        self.assertEqual(model.generate('合成提示词',records.append),'{}')
        self.assertEqual(records[0]['usage'],dict(total_tokens=8));self.assertNotIn('DO_NOT_SAVE',str(records))
        inherited=replace(self.settings,topic_summary_model='explicit-role',upstream_base_url='https://main.example.test/v1',upstream_api_key='synthetic-key',access_key_master=Fernet.generate_key().decode())
        self.assertEqual(TopicSummaryModel(inherited).role.base_url,inherited.upstream_base_url)
