import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from memory_core.archive_policy import ArchiveSettings
from memory_core.archives import ArchiveStore
from memory_core.clock import format_timestamp
from memory_core.config import Settings
from memory_core.db import connect
from memory_core.install import install
from memory_core.postgres import PostgresStore
from memory_core.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_archives import NOW, seed_receipt
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.daily_jobs import DailyJobs
from memory_web.install import install_web
from memory_web.password import write_password
from memory_web.testing.diary_model import SyntheticDiaryModel

ORIGIN='https://memory.example.test'


class ArchiveWebTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        self.now=NOW
        self.core=Settings(3)
        install(self.owner,self.core)
        install_web(self.owner)
        for space in ('one','two'):
            SpaceConfiguration(self.owner).configure(space,Participant('Synthetic Reader'),Participant('Synthetic Assistant'))
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        password=Path(folder.name)/'password.hash'
        write_password(password,'synthetic password')
        self.settings=WebSettings(self.app,self.owner,ORIGIN,password)
        self.application=create_app(self.settings,daily_clock=lambda:self.now)
        self.http=TestClient(self.application,base_url=ORIGIN)
        self.addCleanup(self.http.close)
        login=self.http.post('/api/login',headers={'Origin':ORIGIN},json={'password':'synthetic password'})
        self.headers={'Origin':ORIGIN,'X-CSRF-Token':login.json()['csrf']}

    def tearDown(self):
        pass

    def card(self, archived_at=None, *, space='one'):
        mid=PostgresStore(self.app,space).write_card('合成卡片完整正文',[1,0,0],title='合成归档标题',source_date='1900-01-01')
        with connect(self.app) as c:
            c.execute('UPDATE memory_core.memory_items SET created_at=%s,decay_layer=%s WHERE id=%s',
                ((NOW-timedelta(days=120)).isoformat(),'tier_4_episode',mid))
            if archived_at:
                c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=%s WHERE id=%s",(archived_at,mid))
        return mid

    def row(self,mid):
        with connect(self.app) as c:
            return c.execute('SELECT * FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone()

    def test_archive_months_use_current_archive_time_and_restore_does_not_call_models(self):
        october=self.card('2026-10-01T00:00:00+08:00')
        self.card('2026-09-30T23:59:59+08:00')
        self.card('2026-10-15T12:00:00+08:00',space='two')
        self.card()
        base='/api/spaces/one'
        months=self.http.get(base+'/archive-months').json()
        self.assertEqual(months,[dict(month='2026-10',cards=1),dict(month='2026-09',cards=1)])
        cards=self.http.get(base+'/cards?archive=archived&archive_month=2026-10').json()
        self.assertEqual([r['id'] for r in cards['items']],[october])
        self.assertEqual(cards['items'][0]['source_date'],'1900-01-01')
        path=base+f'/cards/{october}'
        before=self.row(october)
        from memory_core.completion_model import CompletionModel
        from memory_core.http_models import HTTPRecallModels
        with patch.object(HTTPRecallModels,'embed',side_effect=AssertionError('no model call')),patch.object(CompletionModel,'complete_sync',side_effect=AssertionError('no model call')):
            response=self.http.post(path+'/restore',headers=self.headers,json={'expected_archived_at':before['archived_at']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['restored_at'],format_timestamp(NOW,self.core))
        self.assertEqual(self.row(october)['created_at'],before['created_at'])
        self.assertEqual(self.http.get(base+'/cards?archive=archived&archive_month=2026-10').json()['total'],0)
        self.assertEqual(self.http.get(base+'/archive-months').json(),[dict(month='2026-09',cards=1)])
        self.assertEqual(self.http.get(base+'/cards').json()['total'],2)
        self.now+=timedelta(days=1)
        repeated=self.http.post(path+'/restore',headers=self.headers,json={'expected_archived_at':before['archived_at']})
        self.assertFalse(repeated.json()['restored'])
        self.assertEqual(self.row(october)['restored_at'],format_timestamp(NOW,self.core))

    def test_restore_requires_admin_csrf_space_and_current_archive_snapshot(self):
        mid=self.card('2026-10-01T00:00:00+08:00')
        path=f'/api/spaces/one/cards/{mid}/restore'
        payload=dict(expected_archived_at='2026-09-01T00:00:00+08:00')
        self.assertEqual(self.http.post(path,headers=self.headers,json=payload).status_code,409)
        self.assertEqual(self.http.post(path,json=payload).status_code,403)
        payload['expected_archived_at']='2026-10-01T00:00:00+08:00'
        self.assertEqual(self.http.post(path.replace('/one/','/two/'),headers=self.headers,json=payload).status_code,400)
        self.assertEqual(self.http.post(path,headers=self.headers,json=payload|dict(content='forbidden')).status_code,422)
        self.http.cookies.clear()
        self.assertEqual(self.http.get('/api/spaces/one/archive-months').status_code,401)
        self.assertEqual(self.row(mid)['archive_reason'],'decay')

    def test_month_filters_are_validated_and_paginated(self):
        for _ in range(3):
            self.card('2026-10-01T00:00:00+08:00')
        base='/api/spaces/one/cards'
        for query in ('archive=archived&archive_month=2026-13','archive=active&archive_month=2026-10','archive=archived&archive_month=0000-01'):
            self.assertIn(self.http.get(base+'?'+query).status_code,(400,422))
        result=self.http.get(base+'?archive=archived&archive_month=2026-10&page=2&size=2').json()
        self.assertEqual((result['total'],len(result['items'])),(3,1))

    def test_overview_exposes_insufficient_samples_low_rate_and_recovery(self):
        path='/api/spaces/one/overview'
        self.assertEqual(self.http.get(path).json()['archive_health']['reason'],'insufficient_samples')
        for i in range(20):
            seed_receipt(self.app,status='unreported' if i<10 else 'reported_unused')
        self.assertEqual(self.http.get(path).json()['archive_health']['reason'],'low_receipt_rate')
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.conversation_requests SET usage_receipt='{\"status\":\"reported_unused\"}'")
        health=self.http.get(path).json()['archive_health']
        self.assertFalse(health['paused'])
        self.assertEqual((health['rate'],health['injected_rounds']),(1,20))

    def make_jobs(self):
        return DailyJobs(self.settings,self.core,model=SyntheticDiaryModel(),clock=lambda:self.now)

    def tick(self,jobs):
        async def run():
            await jobs.tick()
            await jobs.drain()
        asyncio.run(run())

    def task(self):
        with connect(self.app) as c:
            return c.execute("SELECT * FROM memory_core.daily_tasks WHERE bot_id='one' AND task_kind='archive' ORDER BY business_date DESC LIMIT 1").fetchone()

    def healthy(self):
        for _ in range(20):
            seed_receipt(self.app)

    def test_daily_order_after_extraction_and_approval_restart_and_no_model_needed(self):
        mid=self.card()
        self.healthy()
        jobs=self.make_jobs()
        jobs.extraction_models=SimpleNamespace(configured=True)
        order=[]
        async def completed(task,store,tasks,now):
            tasks.finish(task,now=now)
        jobs.handlers['diary']=completed
        jobs.handlers['extraction']=completed
        for kind,handler in list(jobs.handlers.items()):
            async def traced(task,store,tasks,now,kind=kind,handler=handler):
                order.append(kind)
                await handler(task,store,tasks,now)
            jobs.handlers[kind]=traced
        self.tick(jobs)
        self.assertEqual(order,['recent_notes','diary','extraction','approval','archive','cleanup'])
        self.assertEqual(self.row(mid)['archive_reason'],'decay')
        self.assertEqual(self.task()['result']['archived_count'],1)
        self.tick(self.make_jobs())
        self.assertEqual(self.task()['attempts'],1)

    def test_imported_cards_without_conversation_are_checked_and_pause_then_resume(self):
        mid=self.card()
        jobs=self.make_jobs()
        self.tick(jobs)
        self.assertEqual(self.task()['state'],'skipped')
        self.assertIsNone(self.row(mid)['archive_reason'])
        self.healthy()
        self.now+=timedelta(days=1)
        self.tick(jobs)
        self.assertEqual(self.row(mid)['archive_reason'],'decay')

    def test_failed_archival_retains_card_then_uses_existing_retry(self):
        mid=self.card()
        self.healthy()
        jobs=self.make_jobs()
        def fault(stage,c):
            if stage=='archive':
                c.execute('SELECT 1/0')
        with patch.object(ArchiveStore,'_checkpoint',side_effect=fault):
            self.tick(jobs)
        self.assertEqual(self.task()['state'],'failed')
        self.assertIsNone(self.row(mid)['archive_reason'])
        self.now+=timedelta(minutes=4)
        self.tick(jobs)
        self.assertEqual(self.task()['attempts'],1)
        self.now+=timedelta(minutes=2)
        self.tick(jobs)
        self.assertEqual(self.task()['state'],'done')
        self.assertEqual(self.task()['attempts'],2)
        self.assertEqual(self.row(mid)['archive_reason'],'decay')

    def test_two_workers_only_archive_once(self):
        mid=self.card()
        self.healthy()
        first,second=self.make_jobs(),self.make_jobs()
        entered,release=Event(),Event()
        self.addCleanup(release.set)
        def block(stage,c):
            if stage=='before_lock':
                entered.set()
                if not release.wait(8):
                    raise TimeoutError('synthetic archive wait')
        with patch.object(ArchiveStore,'_checkpoint',side_effect=block),ThreadPoolExecutor(max_workers=2) as pool:
            running=pool.submit(self.tick,first)
            self.assertTrue(entered.wait(4))
            pool.submit(self.tick,second).result(timeout=3)
            release.set()
            running.result(timeout=10)
        self.assertEqual(self.row(mid)['archive_reason'],'decay')
        self.assertEqual(self.task()['attempts'],1)
        self.assertEqual(self.task()['result']['archived_count'],1)
