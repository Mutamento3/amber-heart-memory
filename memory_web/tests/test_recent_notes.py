"""Recent-note integration with synthetic data, HTTP providers and chat only."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from memory_core.bookmarks.bookmarks import BookmarkStore
from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.bookmarks.recent_notes import RecentNotesStore
from memory_core.bookmarks.recent_note_text import SECTIONS
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.testing.provider_server import ProviderServer
from memory_core.tests.support import DatabaseTestCase
from memory_web.admin.access_keys import AccessKeys
from memory_web.app import create_app
from memory_web.mcp.bookmark_tool import DESCRIPTION, BookmarkArguments
from memory_web.config import WebSettings
from memory_web.jobs.daily_jobs import DailyJobs
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.models.recent_note_model import RecentNoteTidyModel
from memory_web.testing.diary_model import SyntheticDiaryModel
from memory_web.testing.fake_upstream import FakeUpstream
from memory_web.testing.recall_models import SyntheticRecallModels

ORIGIN = 'https://memory.example.test'
NOW = datetime.fromisoformat('2026-11-09T03:20:00+08:00')


class SyntheticSecretary:
    configured = True

    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.payload = dict(suggestions=[dict(type='精简',targets=['健康1'],new_text='2026年11月8日 已复查',reason='去掉重复')],need_confirm=[])

    async def generate(self, prompt, material):
        self.calls.append((prompt,material))
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(.01)
        return dict(content=json.dumps(self.payload,ensure_ascii=False),model='synthetic-secretary',usage=dict(total_tokens=10))


class RecentNoteConfigTests(unittest.TestCase):
    def test_medium_reasoning_uses_the_selected_providers_fields(self):
        for host, expected in (
            ('openrouter.ai', {'reasoning': {'effort': 'medium'}, 'max_tokens': 32000}),
            ('api.deepseek.com', {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'medium', 'max_tokens': 32000}),
            ('api.siliconflow.cn', {'enable_thinking': True, 'reasoning_effort': 'medium', 'max_tokens': 32000}),
            ('api.openai.com', {'reasoning_effort': 'medium', 'max_completion_tokens': 32000}),
            ('provider.example.test', {'max_tokens': 32000}),
        ):
            with self.subTest(host=host):
                settings = WebSettings('synthetic', 'owner', ORIGIN, Path('/tmp/synthetic.hash'),
                    recent_notes_tidy_model='synthetic-secretary', recent_notes_tidy_base_url=f'https://{host}/v1',
                    recent_notes_tidy_api_key='synthetic-key')
                def handler(request):
                    payload = json.loads(request.content)
                    options = {key: value for key, value in payload.items() if key not in ('model', 'stream', 'messages')}
                    self.assertEqual(options, expected)
                    return httpx.Response(200, json=dict(choices=[
                        dict(message=dict(content='{}'), finish_reason='stop')]))
                model = RecentNoteTidyModel(settings, transport=httpx.MockTransport(handler))
                self.assertEqual(asyncio.run(model.generate('规则', '材料'))['content'], '{}')

    def test_environment_defaults_overrides_and_invalid_policies(self):
        required = dict(MEMORY_DSN='synthetic',MEMORY_CONFIG_DSN='synthetic-owner',
            MEMORY_WEB_ORIGIN=ORIGIN,MEMORY_WEB_PASSWORD_FILE='/tmp/synthetic.hash')
        with patch.dict('os.environ',required,clear=True):
            settings = WebSettings.from_env()
        self.assertEqual((settings.recent_notes_soft_limit,settings.recent_notes_target,settings.recent_notes_tidy_timeout_seconds),(3500,3000,300))
        env = required | dict(MEMORY_RECENT_NOTES_SOFT_LIMIT='4000',MEMORY_RECENT_NOTES_TARGET='3100',
            MEMORY_RECENT_NOTES_TIDY_MODEL='synthetic-role',MEMORY_RECENT_NOTES_TIDY_TIMEOUT_SECONDS='23')
        with patch.dict('os.environ',env,clear=True):
            settings = WebSettings.from_env()
        self.assertEqual((settings.recent_notes_soft_limit,settings.recent_notes_target,settings.recent_notes_tidy_timeout_seconds),(4000,3100,23))
        for values in [dict(recent_notes_soft_limit=0),dict(recent_notes_target=5000),dict(recent_notes_target=True),
                       dict(recent_notes_tidy_base_url='https://synthetic.example.test'),dict(recent_notes_tidy_timeout_seconds=float('inf'))]:
            with self.assertRaises(ValueError):
                replace(settings,**values)


class RecentNoteWebTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner,Settings(3))
        install_web(self.owner)
        self.configuration = SpaceConfiguration(self.owner)
        for space in ('one','two'):
            self.configuration.configure(space,Participant('小禾'),Participant('林舟'))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password = Path(temporary.name)/'password.hash'
        write_password(password,'synthetic admin')
        master = Fernet.generate_key().decode()
        self.keys = AccessKeys(self.app,self.owner,master)
        self.tokens = {space:self.keys.rotate(space)['key'] for space in ('one','two')}
        self.fake = FakeUpstream()
        self.addCleanup(self.fake.close)
        self.model = SyntheticSecretary()
        self.addCleanup(self.model.release.set)
        self.settings = WebSettings(self.app,self.owner,ORIGIN,password,access_key_master=master,
            upstream_base_url=self.fake.url,upstream_api_key='synthetic-upstream')
        # Each test explicitly controls daily ticks; no race with the poll loop.
        poll = patch.object(DailyJobs,'start',new=AsyncMock())
        poll.start()
        self.addCleanup(poll.stop)
        self.application = create_app(self.settings,recall_models=SyntheticRecallModels(),
            recent_note_models=self.model,daily_clock=lambda:NOW)
        self.http = TestClient(self.application,base_url=ORIGIN,follow_redirects=False)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__,None,None,None)
        self.path = '/api/spaces/one/recent-notes'
        self.notes = RecentNotesStore(self.app,'one')
        self.bookmarks = BookmarkStore(self.app,'one')
        self.csrf = None

    def tearDown(self):
        pass

    def auth(self,space='one'):
        return {'Authorization':'Bearer '+self.tokens[space]}

    def login(self):
        response = self.http.post('/api/login',headers={'Origin':ORIGIN},json=dict(password='synthetic admin'))
        self.assertEqual(response.status_code,200,response.text)
        self.csrf = response.json()['csrf']

    def write(self,path,body=None,method='POST'):
        return self.http.request(method,path,headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf},json=body or {})

    def read(self):
        response = self.http.get(self.path)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def edit(self,section,content):
        response = self.write(self.path,dict(sections={section:content},expected_version=self.read()['version']),method='PUT')
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def mark(self,space='one',**args):
        response = self.http.post('/mcp',headers={**self.auth(space),'Accept':'application/json, text/event-stream'},
            json=dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='mark_moment',arguments=args)))
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['result']

    def start(self):
        response = self.write(self.path+'/tidy/start')
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def latest(self):
        return self.http.get(self.path+'/tidy').json()['job']

    def drain(self):
        self.http.portal.call(self.application.state.recent_note_tidy.drain)

    def save(self,job,sections=None):
        # The browser computes this text; the backend only checks the version.
        return self.write(self.path,dict(tidy_job_id=job['id'],expected_version=job['base_version'],
            sections=sections if sections is not None else dict(health='2026年11月8日 已复查')),method='PUT')

    def test_bookmark_tool_approved_words_default_add_crud_and_space_isolation(self):
        response = self.http.post('/mcp',headers={**self.auth(),'Accept':'application/json, text/event-stream'},
            json=dict(jsonrpc='2.0',id=1,method='tools/list'))
        definitions = {tool['name']:tool for tool in response.json()['result']['tools']}
        self.assertEqual(definitions['mark_moment']['description'],DESCRIPTION.format(user_name='小禾'))
        self.assertEqual(definitions['mark_moment']['inputSchema']['properties']['content']['anyOf'][0]['type'],'string')
        added = self.mark(section='health',content='2026年11月8日\n已经复查')
        self.assertFalse(added['isError'])
        identifier = added['structuredContent']['id']
        self.assertNotIn('\n',added['structuredContent']['content'])
        self.assertEqual(self.mark('two',action='list')['structuredContent']['items'],[])
        self.assertTrue(self.mark('two',action='update',ids=[identifier],content='不能越权')['isError'])
        self.assertFalse(self.mark(action='update',ids=[identifier],section='daily',content='2026年11月8日 新结论')['isError'])
        self.assertFalse(self.mark(action='delete',ids=[identifier])['isError'])
        missing = self.mark(action='update',ids=[identifier],content='新进展')
        self.assertTrue(missing['isError'])
        self.assertIn('已经整理',str(missing))

    def test_management_requires_admin_cookie_origin_and_csrf_not_space_key(self):
        for path in (self.path,self.path+'/tidy','/api/spaces/one/bookmarks'):
            self.assertEqual(self.http.get(path,headers=self.auth()).status_code,401)
        self.login()
        self.assertEqual(self.http.put(self.path,headers={'Origin':ORIGIN},json=dict(sections={'health':'事实'},expected_version=0)).status_code,403)
        self.assertEqual(self.http.put(self.path,headers={'Origin':'https://other.example.test','X-CSRF-Token':self.csrf},json=dict(sections={'health':'事实'},expected_version=0)).status_code,403)
        self.assertEqual(self.http.post('/mcp',json={}).status_code,401)
        self.edit('health','甲 乙\n丙\ufeff')
        read = self.read()
        self.assertEqual(read['counts']['health'],3)
        self.assertEqual(read['total_chars'],3)
        self.assertFalse(read['over_limit'])
        self.assertEqual(self.write(self.path,dict(sections={'health':'旧版本覆盖'},expected_version=0),method='PUT').status_code,409)
        self.assertEqual(self.http.get('/api/spaces/missing/recent-notes').status_code,404)

    def test_core_memory_admin_revision_credentials_and_mcp_origin(self):
        path = '/api/spaces/one/core-memory'
        self.assertEqual(self.http.get('/api/session',headers=self.auth()).status_code,401)
        self.assertEqual(self.http.patch(path,headers={**self.auth(),'Origin':ORIGIN},json=dict(expected_revision=1,core_memory='约定')).status_code,401)
        self.login()
        self.assertEqual(self.http.get('/api/session').json()['mcp_url'],ORIGIN+'/mcp')
        result = self.write(path,dict(expected_revision=1,core_memory='关于小禾的长期约定'),method='PATCH')
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['revision'],2)
        self.assertEqual(self.http.get('/api/spaces/two/configuration').json()['core_memory'],'')
        self.assertEqual(self.write(path,dict(expected_revision=1,core_memory='旧版本'),method='PATCH').status_code,409)
        self.assertEqual(self.write(path,dict(expected_revision=2,core_memory='甲'*5001),method='PATCH').status_code,422)
        self.assertEqual(self.write(path,dict(expected_revision=2,core_memory='api_key: sk-'+('x'*32)),method='PATCH').status_code,400)
        self.assertEqual(self.http.get('/api/spaces/one/configuration').json()['core_memory'],'关于小禾的长期约定')

    def test_multisection_save_is_atomic_and_bookmarks_withdraw_pending_only(self):
        self.login()
        response = self.write(self.path,dict(expected_version=0,sections={'health':'甲','daily':'乙'}),method='PUT')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['version'],1)
        response = self.write(self.path,dict(expected_version=0,sections={'health':'不能覆盖','daily':'不能覆盖'}),method='PUT')
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.read()['sections']['health'],'甲')
        self.assertEqual(self.read()['sections']['daily'],'乙')
        item = self.bookmarks.execute(dict(section='daily',content='合成待进条目'))
        path = '/api/spaces/one/bookmarks/'+item['full_id']
        self.assertEqual(self.http.delete(path,headers=self.auth()).status_code,403)
        self.assertEqual(self.write('/api/spaces/two/bookmarks/'+item['full_id'],method='DELETE').status_code,409)
        self.assertEqual(self.write(path,method='DELETE').status_code,200)
        self.assertEqual(self.write(path,method='DELETE').status_code,409)
        self.assertEqual(self.bookmarks.list()['total'],0)

    def test_pending_only_space_nightly_catchup_runs_despite_diary_failure(self):
        self.login()
        books = BookmarkStore(self.app,'two')
        for content,stamp in [('前天','2026-11-07T12:00:00+08:00'),('昨天','2026-11-08T12:00:00+08:00'),('今天','2026-11-09T12:00:00+08:00')]:
            books.execute(dict(section='daily',content=content),now=datetime.fromisoformat(stamp))
        jobs = self.application.state.daily
        self.assertIn('two',jobs.spaces())

        async def fail(*args):
            raise RuntimeError('synthetic diary failure')

        jobs.handlers['diary'] = fail
        self.http.portal.call(partial(jobs.tick,now=NOW))
        self.http.portal.call(jobs.drain)
        notes = self.http.get('/api/spaces/two/recent-notes').json()
        self.assertEqual(notes['sections']['daily'],'昨天\n前天')
        self.assertEqual(notes['version'],1)
        pending = self.http.get('/api/spaces/two/bookmarks').json()
        self.assertEqual([item['content'] for item in pending['items']],['今天'])
        with connect(self.app) as c:
            states = {row['task_kind']:row['state'] for row in c.execute("SELECT task_kind,state FROM memory_core.daily_tasks WHERE bot_id='two'")}
        self.assertEqual(states['recent_notes'],'done')
        self.assertEqual(states['diary'],'failed')

    def test_secretary_start_latest_saves_frontend_text_atomically(self):
        self.login()
        for section in SECTIONS:
            self.edit(section,'2026年11月8日 已复查，已复查')
        before = self.read()
        job = self.start()
        self.drain()
        done = self.latest()
        self.assertEqual(done['status'],'done')
        self.assertEqual(self.read()['version'],before['version'])
        prompt, material = self.model.calls[0]
        self.assertIn('小禾',prompt)
        self.assertIn('林舟',prompt)
        self.assertNotIn('{{',prompt)
        self.assertTrue(all('【'+prefix+'1】' in material for prefix in ('周期','健康','长期','日常')))
        self.assertEqual(self.save(job,{'missing':'无效分区'}).status_code,400)
        self.assertEqual(self.write(self.path+'/tidy/save').status_code,404)
        saved = self.save(job)
        self.assertEqual(saved.status_code,200,saved.text)
        read = self.read()
        self.assertEqual(read['sections']['health'],'2026年11月8日 已复查')
        self.assertEqual(read['sections']['daily'],before['sections']['daily'])
        self.assertEqual(read['version'],before['version']+1)
        self.assertEqual(self.latest()['status'],'applied')
        self.assertEqual(self.save(job).status_code,409)

    def test_saving_tidy_rejects_both_manual_and_nightly_version_changes(self):
        self.login()
        self.edit('health','2026年11月8日 已复查，重复信息')
        job = self.start()
        self.drain()
        self.edit('health','用户手改的新内容')
        response = self.save(job)
        self.assertEqual(response.status_code,409)
        self.assertIn('重新整理',response.text)
        self.assertEqual(self.read()['sections']['health'],'用户手改的新内容')
        job = self.start()
        self.drain()
        self.bookmarks.execute(dict(section='health',content='夜里新进的内容'),now=NOW-timedelta(days=1))
        self.http.portal.call(partial(self.application.state.daily.tick,now=NOW))
        self.http.portal.call(self.application.state.daily.drain)
        self.assertEqual(self.save(job).status_code,409)
        self.assertIn('夜里新进的内容',self.read()['sections']['health'])

    def test_cancel_no_duplicate_work_and_late_result_cannot_overwrite(self):
        self.login()
        self.edit('health','原文')
        self.model.release.clear()
        job = self.start()
        self.assertTrue(self.model.started.wait(2))
        repeated = self.start()
        self.assertEqual(repeated['id'],job['id'])
        self.assertEqual(len(self.model.calls),1)
        cancelled = self.write(self.path+'/tidy/cancel')
        self.assertEqual(cancelled.status_code,200,cancelled.text)
        self.assertEqual(self.latest()['status'],'cancelled')
        self.assertFalse(self.notes.finish_tidy(job['id'],dict(status='done',suggestions=[])))
        self.assertEqual(self.read()['sections']['health'],'原文')
        self.model.release.set()
        self.start()
        self.drain()
        self.assertEqual(self.latest()['status'],'done')

    def test_restart_marks_orphan_failed_and_does_not_interrupt_live_worker(self):
        self.login()
        policy = self.application.state.recent_note_tidy.policy
        self.notes.begin_tidy(policy)
        with TestClient(create_app(self.settings),base_url=ORIGIN):
            self.assertEqual(self.latest()['status'],'failed')
        self.model.release.clear()
        self.start()
        self.assertTrue(self.model.started.wait(2))
        with TestClient(create_app(self.settings),base_url=ORIGIN):
            self.assertEqual(self.latest()['status'],'running')
        self.write(self.path+'/tidy/cancel')

    def test_default_http_role_uses_main_upstream_without_injected_model(self):
        self.login()
        self.edit('health','2026年11月8日 复查重复内容')
        with ProviderServer() as provider:
            provider.replies['synthetic-secretary'] = json.dumps(self.model.payload)
            settings = replace(self.settings,upstream_base_url=provider.url,recent_notes_tidy_model='synthetic-secretary')
            application = create_app(settings)
            with TestClient(application,base_url=ORIGIN) as http:
                login = http.post('/api/login',headers={'Origin':ORIGIN},json=dict(password='synthetic admin')).json()
                response = http.post(self.path+'/tidy/start',headers={'Origin':ORIGIN,'X-CSRF-Token':login['csrf']},json={})
                self.assertEqual(response.status_code,200,response.text)
                http.portal.call(application.state.recent_note_tidy.drain)
                self.assertEqual(http.get(self.path+'/tidy').json()['job']['status'],'done')
            self.assertEqual(len(provider.requests),1)
            sent = provider.requests[0]
            self.assertEqual(sent['authorization'],'Bearer synthetic-upstream')
            self.assertEqual(sent['body']['model'],'synthetic-secretary')
            self.assertIn('"suggestions"',sent['body']['messages'][0]['content'])
            self.assertEqual(sent['body']['max_tokens'],32000)

    def test_truncated_refused_or_invalid_secretary_responses_fail_without_applying(self):
        self.login()
        self.edit('health','不能丢掉的原文')
        with ProviderServer() as provider:
            settings = replace(self.settings,upstream_base_url=provider.url,recent_notes_tidy_model='synthetic-secretary')
            application = create_app(settings)
            with TestClient(application,base_url=ORIGIN) as http:
                login = http.post('/api/login',headers={'Origin':ORIGIN},json=dict(password='synthetic admin')).json()
                for finish,content,refusal in [('length','{}',None),('stop',json.dumps(self.model.payload),'refused'),('stop','invalid json',None)]:
                    provider.hook = lambda path,body: (200,dict(choices=[dict(finish_reason=finish,message=dict(content=content,refusal=refusal))]))
                    response = http.post(self.path+'/tidy/start',headers={'Origin':ORIGIN,'X-CSRF-Token':login['csrf']},json={})
                    self.assertEqual(response.status_code,200,response.text)
                    http.portal.call(application.state.recent_note_tidy.drain)
                    self.assertEqual(http.get(self.path+'/tidy').json()['job']['status'],'failed')
                    self.assertEqual(http.get(self.path).json()['sections']['health'],'不能丢掉的原文')

    def test_injection_after_diary_before_summary_is_empty_aware_and_frozen_during_tools(self):
        self.login()
        self.configuration.set_persona('one',expected_revision=1,persona='合成人设')
        self.configuration.set_core_memory('one',expected_revision=2,core_memory='旧版核心约定')
        store = ConversationStore(self.app,'one')
        old = store.begin(dict(model='synthetic',messages=[dict(role='user',content='昨天的合成对话')]),
            dict(operation='new'),now=NOW-timedelta(days=1))
        store.finish(old,content='昨天的合成回答',finish_reason='stop',now=NOW-timedelta(days=1))
        self.application.state.daily.model = SyntheticDiaryModel()
        self.http.portal.call(partial(self.application.state.daily.tick,now=NOW))
        self.http.portal.call(self.application.state.daily.drain)
        self.edit('health','旧版健康近况')
        messages = [dict(role='user',content='继续聊')]
        body = dict(model='synthetic-round',messages=messages)
        first = self.http.post('/v1/chat/completions',headers=self.auth(),json=body)
        self.assertEqual(first.status_code,200,first.text)
        self.http.portal.call(self.application.state.proxy.drain)
        frozen = deepcopy(self.fake.requests[-1]['body']['messages'])
        injection = next(message['content'] for message in frozen if '<bookmarks>' in str(message.get('content')))
        self.assertIn('【健康关注】\n旧版健康近况',injection)
        self.assertNotIn('【月经周期】',injection)
        with connect(self.app) as c:
            manifest = c.execute('SELECT context_manifest FROM memory_core.conversation_turns ORDER BY id DESC LIMIT 1').fetchone()['context_manifest']
        kinds = [section['kind'] for section in manifest['sections']]
        self.assertLess(kinds.index('persona'),kinds.index('core_memory'))
        self.assertLess(kinds.index('core_memory'),kinds.index('diary'))
        self.assertIn('以下是关于小禾的长期事实和你们之间的重要约定，由小禾亲自写下',str(frozen))
        self.assertIn('旧版核心约定',str(frozen))
        self.configuration.set_core_memory('one',expected_revision=3,core_memory='新版核心约定')
        self.assertLess(kinds.index('diary'),kinds.index('recent_notes'))
        self.assertLess(kinds.index('recent_notes'),kinds.index('summary'))
        self.edit('health','用户手改后的新近况')
        messages += [first.json()['choices'][0]['message'],dict(role='tool',tool_call_id='call-0',content='合成工具结果')]
        second = self.http.post('/v1/chat/completions',headers=self.auth(),json=body)
        self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(frozen,self.fake.requests[-1]['body']['messages'][:-2])
        messages += [second.json()['choices'][0]['message'],dict(role='tool',tool_call_id='call-1',content='第二结果')]
        self.http.post('/v1/chat/completions',headers=self.auth(),json=body)
        self.http.portal.call(self.application.state.proxy.drain)
        self.http.post('/v1/chat/completions',headers=self.auth(),json=dict(model='synthetic',messages=[dict(role='user',content='新一轮')]))
        self.assertIn('用户手改后的新近况',str(self.fake.requests[-1]['body']['messages']))
        self.assertIn('新版核心约定',str(self.fake.requests[-1]['body']['messages']))
        self.configuration.set_core_memory('one',expected_revision=4,core_memory='')
        self.edit('health','')
        self.http.post('/v1/chat/completions',headers=self.auth(),json=dict(model='synthetic',messages=[dict(role='user',content='空快照')]))
        self.assertNotIn('<bookmarks>',str(self.fake.requests[-1]['body']['messages']))
        self.assertNotIn('<core_memory>',str(self.fake.requests[-1]['body']['messages']))
