"""HTTP admin boundary and durable worker with synthetic provider responses only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from threading import Event, Lock
from time import sleep
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.imports import ImportStore
from memory_core.pending_approval import PendingApprovalStore
from memory_core.review_policy import ApprovalSettings
from memory_core.install import install
from memory_core.space import Participant, SpaceConfiguration
from memory_core.testing.provider_server import ProviderServer
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_import_formats import claude
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.daily_jobs import DailyJobs
from memory_web.import_jobs import ImportBackoff, ImportJobs
from memory_web.install import install_web
from memory_web.password import write_password
from memory_web.testing.import_model import SyntheticImportHTTP, SyntheticImportFixed
from memory_core.extraction_contract import render_prompt

ORIGIN='https://memory.example.test'


class ImportLoadModelTests(unittest.TestCase):
    def test_load_model_does_not_retain_material_while_unit_fixture_can(self):
        prompt=render_prompt('memory_structure',dict(topics=[dict(id=1)]),
            dict(source_date='2025-03-14',content='合成压测原文。'))
        model=SyntheticImportFixed(record_calls=False)
        for _ in range(3):
            self.assertEqual(json.loads(model.extract(prompt))['title'],'合成记忆标题')
        self.assertEqual(model.responses.calls,[])
        recorded=SyntheticImportFixed()
        recorded.extract(prompt)
        self.assertEqual(len(recorded.responses.calls),1)

    def test_throttled_calls_back_off_and_resume_in_separate_slots(self):
        backoff=ImportBackoff()
        moment=100
        def advance(seconds):
            nonlocal moment
            moment+=seconds
        with patch('memory_web.import_jobs.monotonic',lambda: moment), patch('memory_web.import_jobs.sleep',advance):
            before=backoff.wait()
            backoff.limited(3)
            backoff.limited(3)
            backoff.succeeded(before)
            self.assertEqual(backoff.delay,4)
            generation=backoff.wait()
            self.assertEqual(moment,104)
            backoff.succeeded(generation)
            backoff.wait()
            self.assertEqual(moment,105)
            backoff.wait()
            self.assertEqual(moment,106)


class WebImportTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner,Settings(3))
        install_web(self.owner)
        config=SpaceConfiguration(self.owner)
        for space in ('one','two'):
            config.configure(space,Participant('合成读者'),Participant('合成助手'))
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password=Path(temporary.name)/'password.hash'
        write_password(password,'synthetic admin')
        self.server=ProviderServer()
        self.addCleanup(self.server.close)
        self.responses=SyntheticImportHTTP()
        self.server.hook=self.responses.respond
        self.settings=WebSettings(self.app,self.owner,ORIGIN,password,
            extraction_model='synthetic-import-model',extraction_base_url=self.server.url,
            extraction_api_key='synthetic-provider')
        self.real_import_start=ImportJobs.start
        for cls in (DailyJobs,ImportJobs):
            poll=patch.object(cls,'start',new=AsyncMock())
            poll.start()
            self.addCleanup(poll.stop)
        self.application=create_app(self.settings)
        self.http=TestClient(self.application,base_url=ORIGIN)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__,None,None,None)
        login=self.http.post('/api/login',headers={'Origin':ORIGIN},json=dict(password='synthetic admin'))
        self.csrf=login.json()['csrf']
        self.path='/api/spaces/one/imports'

    def tearDown(self):
        pass

    def headers(self):
        return {'Origin':ORIGIN,'X-CSRF-Token':self.csrf}

    def upload(self,source='claude',body=None,filename='conversations.json'):
        record=claude()
        record['chat_messages'][0]['text']='合成原话：周末开始学陶艺。'
        data=body if body is not None else json.dumps([record]).encode()
        return self.http.post(self.path+'/upload',params=dict(source=source,filename=filename),
            headers={**self.headers(),'Content-Type':'application/octet-stream'},content=data)

    def post(self,path,body=None):
        return self.http.post(path,headers=self.headers(),json=body or {})

    def drain(self):
        runner=self.application.state.imports
        for _ in range(20):
            if not runner.run_once():
                return
        self.fail('synthetic import did not settle')

    def queue_memories(self, *items, auto_approve=False):
        data='\n'.join(f'## 2025-03-{n:02d}\n{item}\n' for n,item in enumerate(items,1))
        response=self.upload('memory',data.encode(),'memory.md')
        self.assertEqual(response.status_code,200,response.text)
        identifier=response.json()['id']
        endpoint=self.path+'/'+identifier
        confirmed=self.post(endpoint+'/confirm',{'auto_approve':auto_approve}).json()
        self.assertEqual(confirmed['status'],'queued')
        self.assertEqual(confirmed['auto_approve'],auto_approve)
        return identifier, endpoint

    @staticmethod
    def prompt_material(body):
        return json.loads(body['messages'][0]['content'].rsplit('\n',2)[1])

    def test_admin_upload_preview_confirm_and_default_http_model_to_pending(self):
        response=self.upload()
        self.assertEqual(response.status_code,200,response.text)
        job=response.json()
        endpoint=self.path+'/'+job['id']
        self.assertEqual(self.responses.calls,[])
        plan=self.post(endpoint+'/preview').json()
        self.assertEqual((plan['day_count'],plan['message_count']),(1,2))
        self.assertEqual(self.post(endpoint+'/confirm').status_code,200)
        self.drain()
        result=self.http.get(endpoint).json()
        self.assertEqual((result['status'],result['model_calls']),('completed',2))
        drafts=self.http.get('/api/spaces/one/drafts?status=pending').json()
        self.assertEqual(drafts['total'],1)
        self.assertEqual(self.responses.calls[0][0],'selection')
        with connect(self.app) as c:
            for table in ('conversation_turns','conversation_messages','conversation_summaries','conversation_diaries'):
                self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.'+table).fetchone()['n'],0,table)
        self.assertEqual(self.http.get('/imports').status_code,200)

    def test_progress_estimate_measures_only_the_current_import_run(self):
        clock = [datetime(2026, 4, 1, tzinfo=timezone.utc)]
        self.application.state.imports.clock = lambda: clock[0]
        store = ImportStore(self.app, 'one')
        key = 'estimated_remaining_seconds'

        def advance(seconds):
            clock[0] += timedelta(seconds=seconds)

        data = '\n'.join(f'## 2025-03-{day:02d}\n合成计时材料 {day}。\n' for day in range(1, 6))
        preview = self.upload('memory', data.encode(), 'estimate.md').json()
        identifier = preview['id']
        endpoint = self.path + '/' + identifier

        def progress():
            return self.http.get(endpoint).json()['progress']

        self.assertNotIn(key, preview['progress'])
        advance(4 * 3600)
        self.assertNotIn(key, self.post(endpoint + '/confirm').json()['progress'])
        advance(10)
        first = store.prepare_next(identifier, now=clock[0])
        second = store.prepare_next(identifier, now=clock[0])
        self.assertIsNone(progress()[key])
        advance(110)
        store.finish_item(first, now=clock[0])
        self.assertEqual(progress()[key], 480)
        advance(30)
        store.record_usage(identifier, {'stage': 'structure'}, now=clock[0])
        store.finish_item(second, now=clock[0], error='import_structure_failed')
        listed = self.http.get(self.path).json()['jobs'][0]
        self.assertEqual(listed['progress'][key], 225)

        self.assertNotIn(key, self.post(endpoint + '/stop').json()['progress'])
        advance(2 * 3600)
        self.assertNotIn(key, self.post(endpoint + '/retry').json()['progress'])
        retried = store.prepare_next(identifier, now=clock[0])
        self.assertEqual(retried['id'], second['id'])
        self.assertIsNone(progress()[key])
        advance(20)
        store.finish_item(retried, now=clock[0])
        store = ImportStore(self.app, 'one')
        store.recover_running(identifier, now=clock[0])
        self.assertEqual(progress()[key], 60)
        advance(10)
        self.assertEqual(progress()[key], 90)

        for day in range(3, 6):
            item = store.prepare_next(identifier, now=clock[0])
            error = 'import_structure_failed' if day == 5 else None
            store.finish_item(item, now=clock[0], error=error)
        completed = self.http.get(endpoint).json()
        self.assertEqual(completed['status'], 'completed')
        self.assertNotIn(key, completed['progress'])

        # Retry in the same second as previous completions.
        self.post(endpoint + '/retry')
        self.assertEqual(store.prepare_next(identifier, now=clock[0])['id'], item['id'])
        self.assertIsNone(progress()[key])

    def test_upload_and_other_writes_keep_session_origin_csrf_and_type_guards(self):
        headers={**self.headers(),'Content-Type':'application/octet-stream'}
        path=self.path+'/upload?source=claude&filename=conversations.json'
        for changed,status in [({'Origin':'https://wrong.example.test'},403),({'X-CSRF-Token':'wrong'},403),({'Content-Type':'application/json'},415)]:
            response=self.http.post(path,headers={**headers,**changed},content=b'[]')
            self.assertEqual(response.status_code,status,response.text)
        response=self.http.post(self.path+'/not-upload',headers=headers,content=b'[]')
        self.assertEqual(response.status_code,415)
        self.http.cookies.clear()
        response=self.http.post(path,headers=headers,content=b'[]')
        self.assertEqual(response.status_code,401)

    def test_large_raw_upload_uses_its_own_limit_and_cross_space_cannot_read_job(self):
        record=claude()
        record['chat_messages'][0]['text']='合成原文。'*150000
        response=self.upload(body=json.dumps([record],ensure_ascii=False).encode())
        self.assertEqual(response.status_code,200,response.text)
        identifier=response.json()['id']
        self.assertEqual(self.http.get('/api/spaces/two/imports/'+identifier).status_code,400)
        with patch('memory_web.import_routes.MAX_UPLOAD_BYTES',20):
            self.assertEqual(self.upload().status_code,413)

    def test_memory_has_one_structure_call_and_retains_user_text_date_and_safe_usage(self):
        content='合成记忆：每周学习陶艺。\n\n不改变第二段。'
        response=self.upload('memory',('## 2025-03-14\n'+content).encode(),'memory.md')
        endpoint=self.path+'/'+response.json()['id']
        self.post(endpoint+'/confirm')
        self.drain()
        result=self.http.get(endpoint).json()
        self.assertEqual((result['status'],result['model_calls']),('completed',1))
        self.assertNotIn('usage',result)
        with connect(self.app) as c:
            usage=c.execute('SELECT usage FROM memory_core.import_jobs WHERE id=%s',(response.json()['id'],)).fetchone()['usage']
        self.assertNotIn('DO_NOT_SAVE',json.dumps(usage))
        self.assertEqual(usage[0]['usage']['total_tokens'],20)
        draft=ImportStore(self.app,'one').list()[0]
        self.assertEqual((draft['payload']['content'],draft['payload']['source_date']),(content,'2025-03-14'))
        self.assertEqual([stage for stage,_ in self.responses.calls],['structure'])

    def test_failure_retry_new_worker_does_not_repeat_saved_selection(self):
        response=self.upload()
        endpoint=self.path+'/'+response.json()['id']
        self.post(endpoint+'/confirm')
        self.responses.failures['structure']=1
        self.drain()
        failed=self.http.get(endpoint).json()
        self.assertEqual((failed['status'],failed['progress']['failed']),('completed',1))
        self.post(endpoint+'/retry')
        self.application.state.imports=ImportJobs(self.settings)
        self.drain()
        self.assertEqual(self.http.get(endpoint).json()['status'],'completed')
        self.assertEqual([stage for stage,_ in self.responses.calls],['selection','structure','structure'])

    def test_direct_import_uses_review_pipeline_and_exposes_retained_original(self):
        class Embedder:
            def embed(self,text):
                return [1,0,0]
        original='我和合成助手商量，周末练习陶艺。'
        rewritten='合成读者和合成助手商定周末练习陶艺。'
        def respond(path,body):
            code,value=self.responses.respond(path,body)
            message=value['choices'][0]['message']
            message['content']=json.dumps({**json.loads(message['content']),'content':rewritten})
            return code,value
        self.server.hook=respond
        runner=self.application.state.imports
        runner.embedder=Embedder()
        runner.embedding_model='synthetic-import-embedding'
        identifier,endpoint=self.queue_memories(original,auto_approve=True)
        self.assertTrue(self.post(endpoint+'/confirm',{'auto_approve':False}).json()['auto_approve'])
        self.drain()
        self.assertEqual(self.http.get(endpoint).json()['status'],'completed')
        store=ImportStore(self.app,'one')
        draft=store.list()[0]
        self.assertEqual(draft['status'],'approved')
        self.assertEqual(draft['review_key'],f'import-auto:{draft["id"]}')
        result=self.http.get(f'/api/spaces/one/cards/{draft["memory_item_id"]}').json()
        self.assertEqual(result['content'],rewritten)
        self.assertEqual(result['import_original']['text'],original)
        self.assertEqual(self.http.get('/api/spaces/one/drafts?status=pending&all_items=true').json()['total'],0)
        with connect(self.app) as c:
            self.assertGreater(c.execute('SELECT count(*) AS n FROM memory_core.memory_keyword_tokens').fetchone()['n'],0)
        store.delete_material(store.extraction(draft['extraction_id'])['material_id'])
        result=self.http.get(f'/api/spaces/one/cards/{draft["memory_item_id"]}').json()
        self.assertIsNone(result['import_original']['text'])
        self.assertIsNotNone(result['import_original']['deleted_at'])

    def test_direct_import_approval_failure_waits_for_manual_review(self):
        _,endpoint=self.queue_memories('合成待审核材料。',auto_approve=True)
        self.drain()  # This application has no embedding role configured.
        self.assertEqual(self.http.get(endpoint).json()['status'],'completed')
        store=ImportStore(self.app,'one')
        draft=store.list()[0]
        self.assertEqual((draft['status'],draft['error_code']),('pending','import_approval_failed'))
        due=PendingApprovalStore(self.app,'one',ApprovalSettings()).due(datetime(2050,1,1,tzinfo=timezone.utc))
        self.assertEqual(due,[])
        self.assertEqual(self.http.get('/api/spaces/one/drafts?all_items=true').json()['total'],1)

    def test_retry_restructures_only_the_selected_failed_draft(self):
        def invalid(path,body):
            return 200,dict(choices=[dict(message=dict(content='{}'),finish_reason='stop')])
        self.server.hook=invalid
        self.queue_memories('合成失败甲。','合成失败乙。',auto_approve=True)
        self.drain()
        store=ImportStore(self.app,'one')
        first,second=store.list()
        self.assertEqual(first['error_code'],'structure:format')
        self.server.hook=self.responses.respond
        endpoint=f'/api/spaces/one/drafts/{first["id"]}/restructure'
        response=self.post(endpoint,{'expected_version':first['version']})
        self.assertEqual(response.status_code,202,response.text)
        first_saved=self.http.get(f'/api/spaces/one/drafts/{first["id"]}').json()['draft']
        second_saved=self.http.get(f'/api/spaces/one/drafts/{second["id"]}').json()['draft']
        self.assertEqual(first_saved['status'],'pending')
        self.assertEqual(second_saved['version'],second['version'])
        self.assertEqual([stage for stage,_ in self.responses.calls],['structure'])
        self.assertEqual(self.post(endpoint,{'expected_version':first['version']}).status_code,409)

    def test_failed_item_does_not_stop_the_batch_and_retry_only_repeats_that_item(self):
        bad='合成失败材料。'
        good='合成正常材料。'
        identifier,endpoint=self.queue_memories(bad,good,'合成最后材料。')
        calls=[]
        lock=Lock()
        def respond(path,body):
            content=self.prompt_material(body)['content']
            with lock:
                calls.append(content)
                if content==bad and calls.count(bad)<=2:
                    # Valid HTTP completion, invalid structure: both attempts fail.
                    return 200,dict(choices=[dict(message=dict(content='{}'),finish_reason='stop')])
            return self.responses.respond(path,body)
        self.server.hook=respond
        self.application.state.imports=ImportJobs(replace(self.settings,import_concurrency=1))
        self.drain()
        result=self.http.get(endpoint).json()
        self.assertEqual((result['status'],result['progress']['processed'],result['progress']['failed'],result['model_calls']),
            ('completed',2,1,4))
        self.assertEqual(calls,[bad,bad,good,'合成最后材料。'])
        self.assertEqual(self.post(endpoint+'/retry').json()['status'],'queued')
        self.drain()
        result=self.http.get(endpoint).json()
        self.assertEqual((result['status'],result['progress']['processed'],result['progress']['failed'],result['model_calls']),
            ('completed',3,0,5))
        self.assertEqual(self.post(endpoint+'/retry').json()['status'],'completed')
        self.assertCountEqual(calls,[bad,bad,bad,good,'合成最后材料。'])
        with connect(self.app) as c:
            usage=c.execute('SELECT usage FROM memory_core.import_jobs WHERE id=%s',(identifier,)).fetchone()['usage']
        self.assertEqual(len(usage),5)
        self.assertEqual(len({event['item_id'] for event in usage}),3)

    def test_http_timeout_retries_once_in_same_item(self):
        _,endpoint=self.queue_memories('合成超时材料。')
        calls=[]
        def respond(path,body):
            calls.append(body)
            if len(calls)==1:
                sleep(.65)
            return self.responses.respond(path,body)
        self.server.hook=respond
        self.application.state.imports=ImportJobs(replace(self.settings,extraction_timeout_seconds=.25))
        self.drain()
        result=self.http.get(endpoint).json()
        self.assertEqual((result['status'],result['progress']['failed'],result['model_calls']),('completed',0,2))
        self.assertEqual(len(calls),2)

    def test_stop_while_two_workers_are_in_flight_then_resume_unclaimed_items(self):
        materials=('合成在途甲。','合成在途乙。','合成排队丙。','合成排队丁。')
        identifier,endpoint=self.queue_memories(*materials)
        calls=[]
        lock, started, release=Lock(), Event(), Event()
        def respond(path,body):
            with lock:
                calls.append(self.prompt_material(body)['content'])
                if len(calls)==2:
                    started.set()
            if not release.wait(12):
                return 503,{'error':{'code':'synthetic_wait_expired'}}
            return self.responses.respond(path,body)
        self.server.hook=respond
        runner=ImportJobs(replace(self.settings,import_concurrency=2))
        with ThreadPoolExecutor(max_workers=1) as pool:
            working=pool.submit(runner.run_once)
            try:
                self.assertTrue(started.wait(10))
                running=self.http.get(endpoint).json()
                self.assertEqual((running['status'],running['progress']['running']),('running',2))
                self.assertFalse(ImportJobs(self.settings).run_once())
                rejected=self.http.post(endpoint+'/stop',headers={**self.headers(),'X-CSRF-Token':'bad'},json={})
                self.assertEqual(rejected.status_code,403)
                stopped=self.post(endpoint+'/stop').json()
                self.assertEqual((stopped['status'],stopped['error_code']),('failed','import_stopped'))
            finally:
                release.set()
            self.assertTrue(working.result(timeout=15))
        stopped=self.http.get(endpoint).json()
        self.assertEqual((stopped['status'],stopped['error_code'],stopped['progress']['processed'],stopped['progress']['running']),
            ('failed','import_stopped',2,0))
        self.assertCountEqual(calls,materials[:2])
        self.assertEqual(self.http.get('/api/spaces/one/drafts?status=pending').json()['total'],2)
        self.assertEqual(self.post(endpoint+'/retry').json()['status'],'queued')
        self.drain()
        result=self.http.get(endpoint).json()
        self.assertEqual((result['status'],result['progress']['processed'],result['model_calls']),('completed',4,4))
        self.assertCountEqual(calls,materials)

    def test_stop_queued_does_not_call_model(self):
        _,endpoint=self.queue_memories('合成尚未领取材料。')
        self.assertEqual(self.post(endpoint+'/stop').json()['error_code'],'import_stopped')
        self.drain()
        self.assertEqual(self.responses.calls,[])
        self.post(endpoint+'/retry')
        self.drain()
        self.assertEqual(self.http.get(endpoint).json()['status'],'completed')

    def test_default_missing_model_allows_preview_and_explicitly_blocks_confirmation(self):
        response=self.upload()
        identifier=response.json()['id']
        self.application.state.imports.model.role=replace(self.application.state.imports.model.role,model=None)
        self.assertEqual(self.post(self.path+'/'+identifier+'/preview').status_code,200)
        self.assertEqual(self.post(self.path+'/'+identifier+'/confirm').status_code,503)
        self.assertEqual(self.http.get(self.path+'/'+identifier).json()['status'],'preview')

    def test_new_worker_start_automatically_recovers_durable_queue(self):
        identifier,endpoint=self.queue_memories('合成已经整理材料。','合成崩溃在途材料。','合成剩余排队材料。')
        store=ImportStore(self.app,'one')
        now=datetime.now(timezone.utc)
        first=store.prepare_next(identifier,now=now)
        second=store.prepare_next(identifier,now=now)
        self.assertNotEqual(first['id'],second['id'])
        model=self.application.state.imports.model
        record=lambda event: store.record_usage(identifier,event,now=now)
        store.extract(first['extraction_id'],selector=None,structurer=model.stage('structure',record))
        self.assertEqual(store.get(identifier)['progress']['running'],2)
        async def recover():
            runner=ImportJobs(self.settings)
            await self.real_import_start(runner)
            try:
                async def finished():
                    while store.get(identifier)['status']!='completed':
                        await asyncio.sleep(.02)
                await asyncio.wait_for(finished(),timeout=8)
            finally:
                await runner.stop()
        asyncio.run(recover())
        result=self.http.get(endpoint).json()
        self.assertEqual((result['progress']['processed'],result['model_calls']),(3,3))
        self.assertCountEqual([material['content'] for _,material in self.responses.calls],
            ['合成已经整理材料。','合成崩溃在途材料。','合成剩余排队材料。'])
