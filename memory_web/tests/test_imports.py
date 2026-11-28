"""HTTP admin boundary and durable worker with synthetic provider responses only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
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

from memory_core.storage.config import Settings
from memory_core.storage.clock import business_day_bounds
from memory_core.conversations.conversations import ConversationStore
from memory_core.conversations.chat_text import render_memories
from memory_core.daily.daily_tasks import DailyTasks
from memory_core.storage.db import background_lock, connect
from memory_core.imports.imports import ImportStore
from memory_core.cards.pending_approval import PendingApprovalStore
from memory_core.cards.review import ReviewStore
from memory_core.cards.review_policy import ApprovalSettings
from memory_core.storage.install import install
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.testing.provider_server import ProviderServer
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_deepseek_import import deepseek_session, deepseek_zip
from memory_core.tests.test_import_formats import claude
from memory_core.tests.test_kelivo_import import kelivo_backup
from memory_core.tests.test_operit_import import operit_chat_export, operit_zip
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.jobs.daily_jobs import DailyJobs
from memory_web.jobs.daily_extraction import DailyExtraction
from memory_web.jobs.import_jobs import ImportBackoff, ImportJobs
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.import_model import SyntheticImportHTTP, SyntheticImportFixed
from memory_core.cards.extraction_contract import render_prompt

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
        with patch('memory_web.jobs.import_jobs.monotonic',lambda: moment), patch('memory_web.jobs.import_jobs.sleep',advance):
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

    def test_discard_preview_removes_job_items_and_staged_material(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        identifier = response.json()['id']
        endpoint = self.path + '/' + identifier
        with connect(self.app) as c:
            staged = c.execute('SELECT segments FROM memory_core.import_items WHERE job_id=%s', (identifier,)).fetchone()
            self.assertTrue(staged['segments'])
        blocked = self.http.post(endpoint + '/discard', headers={'Origin': ORIGIN, 'X-CSRF-Token': 'bad'}, json={})
        self.assertEqual(blocked.status_code, 403)
        discarded = self.post(endpoint + '/discard')
        self.assertEqual(discarded.status_code, 200, discarded.text)
        self.assertEqual(self.http.get(self.path).json()['jobs'], [])
        self.assertEqual(self.responses.calls, [])
        self.assertEqual(list(self.settings.password_file.parent.glob('.import-*')), [])
        with connect(self.app) as c:
            for table in ('import_jobs', 'import_items', 'source_materials'):
                self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.' + table).fetchone()['n'], 0)

        _, active = self.queue_memories('合成已确认待处理材料。')
        self.assertEqual(self.post(active + '/discard').status_code, 409)
        self.assertEqual(self.post(active + '/end').status_code, 409)
        self.assertEqual(self.http.get(active).json()['status'], 'queued')

    def test_end_preserves_approved_and_ready_cards_and_reupload_releases_unfinished(self):
        materials = ('合成已收录材料。', '合成待审核材料。', '合成结构失败材料。', '合成尚未开始材料。')
        identifier, endpoint = self.queue_memories(*materials)
        store = ImportStore(self.app, 'one')
        runner = self.application.state.imports
        for _ in range(2):
            item = store.prepare_next(identifier, now=runner.clock())
            runner._process(store, item, lambda: None)

        class Embedder:
            def embed(self, text):
                return [1, 0, 0]

        first = store.list()[0]
        approved = ReviewStore(self.app, 'one').approve(
            first['id'], expected_version=first['version'], expected_policy_revision=first['policy_revision'],
            request_key='synthetic-review', embedder=Embedder(), embedding_model='synthetic-import-embedding',
        )
        card_endpoint = f'/api/spaces/one/cards/{approved["memory_item_id"]}'
        original = self.http.get(card_endpoint).json()
        kept = store.list()
        self.assertEqual([draft['status'] for draft in kept], ['approved', 'pending'])
        self.server.hook = lambda path, body: (200, dict(choices=[dict(message=dict(content='{}'), finish_reason='stop')]))
        unfinished = store.prepare_next(identifier, now=runner.clock())
        runner._process(store, unfinished, lambda: None)
        self.assertEqual(store.list()[-1]['status'], 'structure_failed')
        self.assertEqual(self.post(endpoint + '/stop').json()['status'], 'failed')

        response = self.post(endpoint + '/end')
        self.assertEqual(response.status_code, 200, response.text)
        ended = response.json()
        self.assertEqual((ended['status'], ended['error_code']), ('completed', 'import_ended'))
        progress = ended['progress']
        self.assertEqual((progress['processed'], progress['total'], progress['failed'], progress['running'],
                          progress['structured_cards']), (2, 4, 0, 0, 2))
        self.assertEqual(self.post(endpoint + '/end').json(), ended)
        self.assertEqual(self.post(endpoint + '/retry').json(), ended)
        self.assertEqual(store.list(), kept)
        self.assertEqual(self.http.get(card_endpoint).json(), original)
        with connect(self.app) as c:
            rows = c.execute("""SELECT status,segments,extraction_id FROM memory_core.import_items
                WHERE job_id=%s ORDER BY ordinal""", (identifier,)).fetchall()
            self.assertEqual(rows[-2:], [dict(status='skipped', segments=None, extraction_id=None)] * 2)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.source_materials').fetchone()['n'], 2)

        data = '\n'.join(f'## 2025-03-{n:02d}\n{material}\n' for n, material in enumerate(materials, 1))
        uploaded = self.upload('memory', data.encode(), 'memory.md')
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        reopened = self.path + '/' + uploaded.json()['id']
        plan = self.post(reopened + '/preview').json()
        self.assertEqual((plan['skipped_count'], plan['material_count']), (2, 2))
        self.server.hook = self.responses.respond
        self.responses.calls.clear()
        self.assertEqual(self.post(reopened + '/confirm').json()['status'], 'queued')
        self.drain()
        self.assertCountEqual([value['content'] for _, value in self.responses.calls], materials[2:])
        self.assertEqual(store.list()[:2], kept)

    def test_six_cards_wait_without_connections_and_import_reports_live_progress(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        endpoint = self.path + '/' + response.json()['id']
        self.assertEqual(self.post(endpoint + '/confirm').status_code, 200)
        self.responses.candidate_count = 5
        runner = self.application.state.imports
        core = Settings(3)
        now = datetime.now(timezone.utc)
        daily_day = (now.date() - timedelta(days=2))
        instant = business_day_bounds(daily_day, core)[0] + timedelta(hours=1)
        store = ConversationStore(self.app, 'two')
        record = store.begin(dict(model='synthetic', messages=[
            dict(role='user', content='合成原话：今天复习植物。'),
        ]), {'operation': 'new'}, now=instant)
        store.finish(record, content='合成回应', finish_reason='stop', now=instant)
        tasks = DailyTasks(self.app, 'two')

        release = Event()
        import_blocked = Event()
        six_blocked = Event()
        locks = Lock()
        import_waiting = 0
        calls_waiting = 0

        def gate(stage, material):
            nonlocal import_waiting, calls_waiting
            if stage != 'structure':
                return
            nightly = material['business_date'] == daily_day.isoformat()
            if not nightly and material['title'] != '合成第二张':
                return
            with locks:
                calls_waiting += 1
                if not nightly:
                    import_waiting += 1
                    if import_waiting == 4:
                        import_blocked.set()
                if calls_waiting == 6:
                    six_blocked.set()
            if not release.wait(30):
                raise AssertionError('test did not release blocked model calls')

        self.responses.hook = gate

        def run_daily():
            with tasks.run_lock() as acquired:
                self.assertTrue(acquired)
                task = tasks.claim('extraction', daily_day.isoformat(), now=now)
                asyncio.run(DailyExtraction(self.settings, runner.model, lambda: now)(task, None, tasks, now))

        with ThreadPoolExecutor(max_workers=2) as pool:
            importing = pool.submit(runner.run_once)
            try:
                self.assertTrue(import_blocked.wait(8))
                daily = pool.submit(run_daily)
                self.assertTrue(six_blocked.wait(8))
                # The first draft has finished while the others still wait.
                for _ in range(60):
                    response = self.http.get(endpoint)
                    self.assertEqual(response.status_code, 200, response.text)
                    progress = response.json()['progress']
                    if progress['structured_cards'] == 1:
                        break
                    sleep(.05)
                self.assertEqual((progress['total'], progress['processed'], progress['structured_cards']), (1, 0, 1))
                with background_lock(self.app, 'synthetic-other-scheduler') as guard:
                    self.assertIsNotNone(guard)
                    sessions_sql = """SELECT count(*) AS total,
                        count(*) FILTER (WHERE state='idle in transaction') AS idle_transactions
                        FROM pg_stat_activity WHERE datname=current_database()
                          AND backend_type='client backend'"""
                    with connect(self.app) as probe:
                        self.assertEqual(probe.execute(sessions_sql).fetchone(), {'total': 4, 'idle_transactions': 0})
                        claims = probe.execute('''SELECT count(*) AS total FROM memory_core.pending_memories
                            WHERE structure_claim_token IS NOT NULL''').fetchone()['total']
                        self.assertEqual(claims, 6)
                    with ExitStack() as short_queries:
                        first = short_queries.enter_context(connect(self.app))
                        for _ in range(4):
                            short_queries.enter_context(connect(self.app))
                        self.assertEqual(first.execute(sessions_sql).fetchone(), {'total': 8, 'idle_transactions': 0})
            finally:
                release.set()
            self.assertTrue(importing.result(timeout=12))
            daily.result(timeout=12)
        progress = self.http.get(endpoint).json()['progress']
        self.assertEqual((progress['processed'], progress['structured_cards'], progress['failed']), (1, 5, 0))

    def test_deepseek_selects_conversations_and_uses_shared_worker_and_dedup(self):
        backup = deepseek_zip(deepseek_session(), deepseek_session("synthetic-second", "合成另一段对话"))
        response = self.upload("deepseek", backup, "deepseek_data.zip")
        self.assertEqual(response.status_code, 200, response.text)
        endpoint = self.path + "/" + response.json()["id"]
        preview = self.post(endpoint + "/preview").json()
        self.assertEqual((preview["material_count"], preview["message_count"]), (4, 10))
        self.assertTrue(all(chat["selected"] for chat in preview["conversations"]))
        chosen = next(chat["key"] for chat in preview["conversations"] if chat["title"] == "合成植物对话")
        selection = {"selected_conversations": [chosen]}
        self.assertEqual(self.post(endpoint + "/preview", selection).json()["material_count"], 2)
        self.assertEqual(self.responses.calls, [])
        self.assertEqual(self.post(endpoint + "/confirm", selection).status_code, 200)
        self.drain()
        result = self.http.get(endpoint).json()
        self.assertEqual((result["status"], result["model_calls"]), ("completed", 4))
        self.assertEqual((result["progress"]["processed"], result["progress"]["structured_cards"]), (2, 2))
        with connect(self.app) as connection:
            sources = connection.execute("""SELECT source_key,segments FROM memory_core.source_materials
                ORDER BY business_date""").fetchall()
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(row["source_key"].startswith(f"import:deepseek:{chosen}:") for row in sources))
        self.assertEqual([[part["segment_id"] for part in row["segments"]] for row in sources],
                         [["m-1", "m-3"], ["m-5", "m-7", "m-8"]])
        again = self.upload("deepseek", backup, "deepseek_data.zip")
        self.assertEqual(again.status_code, 200, again.text)
        repeated = self.post(self.path + "/" + again.json()["id"] + "/preview", selection).json()
        self.assertEqual((repeated["material_count"], repeated["skipped_count"]), (0, 2))

    def test_deepseek_bad_archive_returns_clear_error_and_rolls_back(self):
        response = self.upload("deepseek", deepseek_zip(version=1), "deepseek_data.zip")
        self.assertEqual(response.status_code, 400)
        self.assertIn("格式版本 3", response.json()["detail"])
        broken = deepseek_session("synthetic-broken")
        del broken["current_message_id"]
        response = self.upload("deepseek", deepseek_zip(deepseek_session(), broken), "deepseek_data.zip")
        self.assertEqual(response.status_code, 400)
        self.assertIn("DeepSeek 导出格式不完整", response.json()["detail"])
        self.assertEqual(self.http.get(self.path).json()["jobs"], [])
        self.assertEqual(self.responses.calls, [])

    def test_kelivo_uses_date_selection_and_existing_worker_and_dedup(self):
        encrypted = self.upload('kelivo', b'KELIVENC' + bytes(64), 'backup.kbackup')
        self.assertEqual(encrypted.status_code, 400)
        self.assertIn('请关闭备份加密后重新备份', encrypted.json()['detail'])
        self.assertEqual(self.http.get(self.path).json()['jobs'], [])

        backup = kelivo_backup()
        response = self.upload('kelivo', backup, 'backup.zip')
        self.assertEqual(response.status_code, 200, response.text)
        endpoint = self.path + '/' + response.json()['id']
        selection = {'date_end': '2025-03-13'}
        plan = self.post(endpoint + '/preview', selection).json()
        self.assertEqual((plan['source'], plan['day_count'], plan['message_count']), ('kelivo', 1, 2))
        self.assertEqual(self.responses.calls, [])

        self.assertEqual(self.post(endpoint + '/confirm', selection).json()['status'], 'queued')
        self.drain()
        job = self.http.get(endpoint).json()
        self.assertEqual((job['status'], job['progress']['processed'], job['model_calls']), ('completed', 1, 2))
        stage, material = self.responses.calls[0]
        self.assertEqual(stage, 'selection')
        self.assertEqual([segment['text'] for segment in material['segments']], [
            '合成原话：一起学陶艺。', '合成选中回复。',
        ])

        second = self.upload('kelivo', backup, 'renamed.zip').json()
        repeated = self.post(self.path + '/' + second['id'] + '/preview', selection).json()
        self.assertEqual((repeated['material_count'], repeated['skipped_count']), (0, 1))

    def test_kelivo_imports_only_conversations_in_selected_assistant_group(self):
        backup = kelivo_backup(multiple_assistants=True)
        response = self.upload('kelivo', backup, 'assistants.zip')
        self.assertEqual(response.status_code, 200, response.text)
        endpoint = self.path + '/' + response.json()['id']
        plan = self.post(endpoint + '/preview').json()
        groups = {group['name']: group['conversations'] for group in plan['assistant_groups']}
        self.assertEqual(set(groups), {'陶艺助手', '写作助手', '未归属'})
        self.assertEqual(plan['material_count'], 0)
        self.assertNotIn('合成助手配置', json.dumps(plan, ensure_ascii=False))
        chosen = groups['陶艺助手']
        self.assertEqual({item['title'] for item in chosen}, {'合成对话', '合成茶杯记录'})
        selection = {'date_end': '2025-03-13', 'selected_conversations': [item['key'] for item in chosen]}
        filtered = self.post(endpoint + '/preview', selection).json()
        self.assertEqual((filtered['material_count'], filtered['message_count']), (2, 3))
        self.assertEqual(self.post(endpoint + '/confirm', selection).json()['status'], 'queued')
        self.drain()
        job = self.http.get(endpoint).json()
        self.assertEqual((job['status'], job['progress']['processed']), ('completed', 2))
        selected_text = [segment['text'] for stage, material in self.responses.calls if stage == 'selection'
                         for segment in material['segments']]
        self.assertCountEqual(selected_text, ['合成原话：一起学陶艺。', '合成选中回复。', '合成原话：我做了一只茶杯。'])

    def test_kelivo_memory_uses_one_card_per_entry_and_stable_dedup(self):
        class Embedder:
            def embed(self, text):
                return [1, 0, 0]

        runner = self.application.state.imports
        runner.embedder = Embedder()
        runner.embedding_model = 'synthetic-import-embedding'
        backup = kelivo_backup(multiple_assistants=True, with_memories=True)
        response = self.upload('kelivo', backup, 'memories.zip')
        self.assertEqual(response.status_code, 200, response.text)
        endpoint = self.path + '/' + response.json()['id']
        initial = self.post(endpoint + '/preview').json()
        groups = {group['name']: group['memories'] for group in initial['memory_groups']}
        self.assertGreater(len(groups['全局记忆'][0]['text']), len(groups['全局记忆'][0]['title']))
        keys = [groups[name][0]['key'] for name in ('陶艺助手', '写作助手')]
        selection = {'selected_memories': keys, 'selected_conversations': [], 'date_end': '2025-03-13'}
        plan = self.post(endpoint + '/preview', selection).json()
        self.assertEqual((plan['memory_count'], plan['message_count'], plan['model_calls_max']), (2, 0, 2))
        self.assertEqual(self.post(endpoint + '/confirm', dict(selection, auto_approve=True)).json()['status'], 'queued')
        self.drain()
        job = self.http.get(endpoint).json()
        self.assertEqual((job['progress']['processed'], job['progress']['failed'], job['model_calls']), (2, 0, 2))
        self.assertCountEqual(self.responses.calls, [
            ('structure', {'content': '合成陶艺记忆。', 'source_date': '2025-03-14'}),
            ('structure', {'content': '合成旧记忆。', 'source_date': None}),
        ])
        with connect(self.app) as c:
            cards = c.execute('SELECT title,content,source_date FROM memory_core.memory_items').fetchall()
        self.assertCountEqual([(card['content'], card['source_date']) for card in cards], [
            ('合成陶艺记忆。', '2025-03-14'), ('合成旧记忆。', None),
        ])
        undated = next(card for card in cards if card['source_date'] is None)
        self.assertIn('日期：未注明', render_memories([dict(undated, number=1)], '合成读者'))

        revised = kelivo_backup(multiple_assistants=True, with_memories=True, revised=True)
        repeated = self.upload('kelivo', revised, 'revised.zip')
        self.assertEqual(repeated.status_code, 200, repeated.text)
        plan = self.post(self.path + '/' + repeated.json()['id'] + '/preview', selection).json()
        self.assertEqual((plan['memory_count'], plan['skipped_count'], plan['model_calls_max']), (0, 2, 0))

    def test_undated_kelivo_memory_rejects_a_date_invented_by_model(self):
        def respond(path, body):
            code, value = self.responses.respond(path, body)
            message = value['choices'][0]['message']
            message['content'] = json.dumps(dict(json.loads(message['content']), source_date='2025-03-14'))
            return code, value

        self.server.hook = respond
        backup = kelivo_backup(multiple_assistants=True, with_memories=True)
        upload = self.upload('kelivo', backup, 'memories.zip')
        self.assertEqual(upload.status_code, 200, upload.text)
        endpoint = self.path + '/' + upload.json()['id']
        plan = self.post(endpoint + '/preview').json()
        old = next(entry for group in plan['memory_groups'] for entry in group['memories']
                   if entry['source_date'] is None)
        self.post(endpoint + '/confirm', {'selected_memories': [old['key']], 'selected_conversations': []})
        self.drain()
        self.assertEqual(self.http.get(endpoint).json()['progress']['failed'], 1)
        draft = ImportStore(self.app, 'one').list()[0]
        self.assertEqual(draft['error_code'], 'structure:format')
        self.assertIsNone(draft['business_date'])
        self.assertIsNone(draft['payload'])

    def test_operit_combined_import_previews_filters_processes_and_deduplicates(self):
        archive = operit_zip()
        response = self.upload('operit', archive, 'both.zip')
        self.assertEqual(response.status_code, 200, response.text)
        endpoint = self.path + '/' + response.json()['id']
        initial = self.post(endpoint + '/preview').json()
        self.assertEqual(initial['material_count'], 0)
        self.assertEqual(self.responses.calls, [])
        chats = {group['name']: group['conversations'] for group in initial['assistant_groups']}
        self.assertEqual(set(chats), {'合成角色卡甲', '合成角色卡乙'})
        memories = initial['memory_groups'][0]['memories']
        self.assertTrue(all(not entry['selected'] for entry in memories))
        selection = dict(
            selected_conversations=[chats['合成角色卡甲'][0]['key']],
            selected_memories=[entry['key'] for entry in memories if entry['title'] in ('合成自动抽取', '合成人工条目')],
            date_end='2025-03-13',
        )
        plan = self.post(endpoint + '/preview', selection).json()
        self.assertEqual((plan['material_count'], plan['message_count'], plan['memory_count']), (3, 2, 2))
        self.assertEqual(self.post(endpoint + '/confirm', selection).json()['status'], 'queued')
        self.drain()
        job = self.http.get(endpoint).json()
        self.assertEqual((job['status'], job['progress']['processed'], job['progress']['failed']), ('completed', 3, 0))
        chat = next(material for stage, material in self.responses.calls if stage == 'selection')
        self.assertEqual([segment['text'] for segment in chat['segments']],
                         ['合成原话：今天开始学陶艺。', '合成选中回复。'])
        self.assertCountEqual([material for stage, material in self.responses.calls
                               if stage == 'structure' and 'title' not in material], [
            {'content': '合成自动抽取\n\n合成记忆正文。', 'source_date': '2025-03-14'},
            {'content': '合成人工条目\n\n合成记忆正文。', 'source_date': None},
        ])
        with connect(self.app) as connection:
            drafts = connection.execute("""SELECT p.payload->>'source_date' AS day
                FROM memory_core.pending_memories p
                JOIN memory_core.material_extractions e ON (p.extraction_id,p.bot_id)=(e.id,e.bot_id)
                JOIN memory_core.source_materials s ON (e.material_id,e.bot_id)=(s.id,s.bot_id)
                WHERE s.material_kind='memory'""").fetchall()
        self.assertCountEqual([draft['day'] for draft in drafts], ['2025-03-14', None])
        repeated = self.upload('operit', archive, 'renamed.zip')
        self.assertEqual(repeated.status_code, 200, repeated.text)
        plan = self.post(self.path + '/' + repeated.json()['id'] + '/preview', selection).json()
        self.assertEqual((plan['material_count'], plan['skipped_count']), (0, 3))

    def test_operit_late_invalid_header_rolls_back_upload(self):
        data = operit_chat_export()
        response = self.upload('operit', json.dumps(dict(
            chats=data['chats'], archiveType=data['archiveType'], formatVersion=3,
        )).encode(), 'chat_backup.json')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('版本 2', response.json()['detail'])
        self.assertEqual(self.http.get(self.path).json()['jobs'], [])
        self.assertEqual(self.responses.calls, [])

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
        with patch('memory_web.admin.import_routes.MAX_UPLOAD_BYTES',20):
            self.assertEqual(self.upload().status_code,413)

    def test_memory_has_one_structure_call_and_retains_user_text_date_and_safe_usage(self):
        content='合成记忆：每周学习陶艺。\n\n不改变第二段。'
        response=self.upload('memory',('## 2025-03-14\n'+content).encode(),'memory.md')
        endpoint=self.path+'/'+response.json()['id']
        self.post(endpoint+'/confirm')
        self.drain()
        result=self.http.get(endpoint).json()
        self.assertEqual((result['status'],result['model_calls']),('completed',1))
        self.assertEqual(result['progress']['structured_cards'],1)
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
        self.assertEqual(failed['progress']['structured_cards'],0)
        self.post(endpoint+'/retry')
        self.application.state.imports=ImportJobs(self.settings)
        self.drain()
        retried=self.http.get(endpoint).json()
        self.assertEqual(retried['status'],'completed')
        self.assertEqual(retried['progress']['structured_cards'],1)
        self.assertEqual([stage for stage,_ in self.responses.calls],['selection','structure','structure'])

    def test_saves_during_selection_and_structure_do_not_pause_import(self):
        config = SpaceConfiguration(self.owner)

        def respond(path, body):
            response = self.responses.respond(path, body)
            stages = [stage for stage, _ in self.responses.calls]
            if stages == ['selection']:
                config.set_core_memory('one', expected_revision=1, core_memory='筛选期间保存的核心记忆。')
            elif stages == ['selection', 'selection', 'structure']:
                config.set_persona('one', expected_revision=2, persona='整理期间保存的人设。')
            return response

        self.server.hook = respond
        response = self.upload()
        endpoint = self.path + '/' + response.json()['id']
        self.assertEqual(self.post(endpoint + '/confirm').json()['status'], 'queued')
        self.drain()

        done = self.http.get(endpoint).json()
        self.assertEqual((done['status'], done['progress']['processed'], done['progress']['failed']), ('completed', 1, 0))
        self.assertIsNone(done['error_code'])
        self.assertEqual([stage for stage, _ in self.responses.calls], ['selection', 'selection', 'structure', 'structure'])
        draft, = ImportStore(self.app, 'one').list()
        self.assertEqual((draft['status'], draft['policy_revision']), ('pending', 3))

    def test_direct_import_uses_review_pipeline_and_exposes_retained_original(self):
        config = SpaceConfiguration(self.owner)

        class Embedder:
            saved = False

            def embed(self,text):
                if not self.saved:
                    config.set_core_memory('one', expected_revision=1, core_memory='直接收录期间保存的核心记忆。')
                    self.saved = True
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
        self.assertEqual([stage for stage, _ in self.responses.calls], ['structure'])
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

        class ProcessExited(BaseException):
            pass

        class InterruptedModel:
            def extract(self, prompt):
                raise ProcessExited()

        with self.assertRaises(ProcessExited):
            store.extract(second['extraction_id'], selector=None, structurer=InterruptedModel())
        orphan = store.extraction(second['extraction_id'])['drafts'][0]
        self.assertIsNotNone(orphan['structure_claim_token'])
        self.assertEqual(store.get(identifier)['progress']['running'],2)

        async def recover():
            runner=ImportJobs(self.settings)
            await self.real_import_start(runner)
            try:
                async def wait_for_progress(processed):
                    while store.get(identifier)['progress']['processed'] != processed:
                        await asyncio.sleep(.02)
                # The restarted runner continues the other materials while
                # the orphaned draft still has a live claim.
                await asyncio.wait_for(wait_for_progress(2),timeout=8)
                self.assertEqual(store.get(identifier)['progress']['failed'],0)
                still_claimed = store.extraction(second['extraction_id'])['drafts'][0]
                self.assertEqual(still_claimed['structure_claim_token'], orphan['structure_claim_token'])
                with connect(self.app) as connection:
                    connection.execute("""UPDATE memory_core.pending_memories
                        SET structure_claim_until=memory_core.format_timestamp(clock_timestamp() - interval '1 second')
                        WHERE id=%s""", (orphan['id'],))
                await asyncio.wait_for(wait_for_progress(3),timeout=8)
            finally:
                await runner.stop()
        asyncio.run(recover())
        result=self.http.get(endpoint).json()
        self.assertEqual((result['progress']['processed'],result['model_calls']),(3,3))
        self.assertIsNone(store.extraction(second['extraction_id'])['drafts'][0]['structure_claim_token'])
        self.assertCountEqual([material['content'] for _,material in self.responses.calls],
            ['合成已经整理材料。','合成崩溃在途材料。','合成剩余排队材料。'])
