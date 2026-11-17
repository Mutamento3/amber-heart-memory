import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from memory_core.conversations.chat_text import IMAGE_DESCRIPTION_PROMPT,IMAGE_PLACEHOLDER
from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.conversations.image_descriptions import validate_results
from memory_core.storage.space import SpaceConfiguration,Participant
from memory_core.testing.images import image_url
from memory_core.testing.provider_server import ProviderServer
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_image_descriptions import result,body
from memory_web.admin.access_keys import AccessKeys
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.models.image_model import ImageModel
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.fake_upstream import FakeUpstream

ORIGIN='https://memory.example.test'


def completion(content='合成方块。',reason='stop',**message):
    return dict(choices=[dict(message=dict(content=content,**message),finish_reason=reason)],
        usage=dict(total_tokens=8,description='BODY_MUST_NOT_PERSIST'))


class ImageModelTests(unittest.TestCase):
    def settings(self,**extra):
        return WebSettings('synthetic-dsn','synthetic-owner',ORIGIN,Path('/tmp/synthetic.hash'),
            access_key_master='synthetic-master',upstream_base_url='https://main.example.test/v1',
            upstream_api_key='synthetic-main-key',image_description_model='synthetic-vision',**extra)
    def run_model(self,settings,handler):
        return asyncio.run(ImageModel(settings,transport=httpx.MockTransport(handler)).describe(
            dict(url=image_url()),IMAGE_DESCRIPTION_PROMPT.format(user_name='合成读者',assistant_name='合成助手')))
    def test_configuration_is_explicit_paired_private_and_default_env_works(self):
        s=self.settings();self.assertTrue(ImageModel(s).configured)
        self.assertFalse(ImageModel(replace(s,image_description_model=None)).configured)
        for kwargs in (dict(image_description_backup_model='backup',image_description_model=None),
            dict(image_description_base_url='https://vision.example.test/v1'),dict(image_description_timeout_seconds=float('nan')),
            dict(image_description_concurrency=0),dict(image_description_max_pending=0)):
            with self.assertRaises(ValueError):replace(s,**kwargs)
        dedicated=replace(s,image_description_base_url='https://vision.example.test/v1',image_description_api_key='synthetic-private-key')
        self.assertNotIn('synthetic-private-key',repr(dedicated))
        env=dict(MEMORY_DSN='synthetic-dsn',MEMORY_CONFIG_DSN='synthetic-owner',MEMORY_WEB_ORIGIN=ORIGIN,
            MEMORY_WEB_PASSWORD_FILE='/tmp/synthetic.hash',MEMORY_IMAGE_DESCRIPTION_MODEL='synthetic-explicit',
            MEMORY_IMAGE_DESCRIPTION_BASE_URL='https://vision.example.test/v1',MEMORY_IMAGE_DESCRIPTION_API_KEY='synthetic-private-key')
        with patch.dict('os.environ',env,clear=True):
            parsed=WebSettings.from_env()
        self.assertEqual(parsed.image_description_model,'synthetic-explicit')
        self.assertEqual(ImageModel(parsed).role.api_key,'synthetic-private-key')
    def test_image_payload_and_names_and_usage_are_preserved_without_secret_text(self):
        sent=[]
        def handler(request):
            sent.append(json.loads(request.content));return httpx.Response(200,json=completion())
        saved=self.run_model(self.settings(),handler)
        content=sent[0]['messages'][0]['content']
        self.assertEqual(content[0],dict(type='image_url',image_url=dict(url=image_url())))
        self.assertIn('合成读者',content[1]['text']);self.assertIn('合成助手',content[1]['text'])
        self.assertEqual(saved['calls'][0]['usage'],dict(total_tokens=8));self.assertNotIn('BODY_MUST_NOT_PERSIST',str(saved))
        self.assertNotIn('data:image',str(saved))
    def test_dedicated_endpoint_and_key_are_used_instead_of_main_upstream(self):
        settings=replace(self.settings(),image_description_base_url='https://vision.example.test/v1',image_description_api_key='synthetic-vision-key')
        def handler(request):
            self.assertEqual(str(request.url),'https://vision.example.test/v1/chat/completions')
            self.assertEqual(request.headers['authorization'],'Bearer synthetic-vision-key')
            return httpx.Response(200,json=completion())
        saved=self.run_model(settings,handler)
        self.assertEqual(saved['description'],'合成方块。')
        self.assertNotIn('synthetic-vision-key',str(saved))

    def test_only_explicit_filtering_or_refusal_calls_backup(self):
        blocked=[completion('',reason='content_filter'),completion(None,refusal='合成拒绝'),
            completion([dict(type='refusal',refusal='合成拒绝')]),dict(error=dict(code='content_filter',message='synthetic-main-key')),
            dict(error=dict(code=403,metadata=dict(error_type='content_policy_violation',raw='synthetic-main-key'))),
            dict(error=dict(code=403,metadata=dict(error_type='refusal'))),
            dict(error=dict(code=403,metadata=dict(reasons=['synthetic moderation'],flagged_input='synthetic-main-key'))),
            dict(choices=[dict(message=dict(content=''),finish_reason='error',error=dict(code=403,metadata=dict(error_type='refusal')))])]
        for index,first in enumerate(blocked):
            with self.subTest(index=index):
                sent=[]
                def handler(request):
                    sent.append(json.loads(request.content)['model'])
                    return httpx.Response(403 if index==3 and len(sent)==1 else 200,json=first if len(sent)==1 else completion('备用的合成描述。'))
                saved=self.run_model(self.settings(image_description_backup_model='synthetic-backup'),handler)
                self.assertEqual(sent,['synthetic-vision','synthetic-backup']);self.assertEqual(saved['description'],'备用的合成描述。')
                self.assertEqual([c['state'] for c in saved['calls']],['blocked','succeeded'])
                self.assertNotIn('synthetic-main-key',str(saved))
    def test_network_http_truncation_and_bad_output_keep_placeholder_without_backup(self):
        for status,value in [(401,dict(error=dict(message='synthetic-main-key'))),(429,{}),(503,{}),
                             (403,dict(error=dict(code=403,metadata=dict(limit_source='openrouter_guardrail_budget')))),
                             (403,dict(error=dict(code=403,metadata=dict(error_type='permission_denied')))),
                             (200,completion('',reason='length')),
                             (200,completion('synthetic-main-key')),(200,completion('data:image/png;base64,REJECT')),(200,{})]:
            with self.subTest(status=status,value=value):
                sent=[]
                def handler(request):sent.append(request);return httpx.Response(status,json=value)
                saved=self.run_model(self.settings(image_description_backup_model='synthetic-backup'),handler)
                self.assertEqual(len(sent),1);self.assertIsNone(saved['description']);self.assertNotIn('synthetic-main-key',str(saved))
    def test_timeout_and_transport_failure_are_safe_and_do_not_retry(self):
        for handler in (lambda request:(_ for _ in ()).throw(httpx.ConnectError('synthetic-main-key')),
                        lambda request:(_ for _ in ()).throw(httpx.ReadTimeout('synthetic-main-key'))):
            saved=self.run_model(self.settings(image_description_backup_model='synthetic-backup'),handler)
            self.assertIsNone(saved['description']);self.assertEqual(len(saved['calls']),1)
            self.assertNotIn('synthetic-main-key',str(saved))
            validate_results([saved],1)
    def test_absolute_timeout_covers_slow_response_and_does_not_use_backup(self):
        async def slow(request):
            await asyncio.sleep(.2)
            return httpx.Response(200,json=completion())
        saved=self.run_model(self.settings(image_description_backup_model='synthetic-backup',image_description_timeout_seconds=.02),slow)
        self.assertIsNone(saved['description'])
        self.assertEqual([c['code'] for c in saved['calls']],['image_description_timeout'])

    def test_two_filtered_models_finish_once_and_identical_backup_is_not_called_twice(self):
        for backup,expected in [('synthetic-backup',2),('synthetic-vision',1)]:
            saved=self.run_model(self.settings(image_description_backup_model=backup),lambda r:httpx.Response(200,json=completion('',reason='content_filter')))
            self.assertIsNone(saved['description']);self.assertEqual(len(saved['calls']),expected)


class ControlledVision:
    configured=True
    def __init__(self):
        self.calls=[];self.started=threading.Event();self.release=threading.Event();self.release.set()
        self.active=0;self.peak=0
    async def describe(self,image,prompt):
        self.calls.append((image,prompt));self.started.set();self.active+=1;self.peak=max(self.peak,self.active)
        try:
            while not self.release.is_set():await asyncio.sleep(.005)
            return result('合成图像的客观描述。')
        finally:self.active-=1


class ImageProxyTests(DatabaseTestCase):
    def setUp(self):
        super().setUp();self.addCleanup(super().tearDown)
        install(self.owner,Settings(3));install_web(self.owner)
        for space in ('one','two'):SpaceConfiguration(self.owner).configure(space,Participant('合成读者'),Participant('合成助手'))
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        password=Path(folder.name)/'password.hash';write_password(password,'synthetic admin')
        self.fake=FakeUpstream();self.addCleanup(self.fake.close)
        self.vision=ControlledVision();self.addCleanup(self.vision.release.set)
        master=Fernet.generate_key().decode()
        self.settings=WebSettings(self.app,self.owner,ORIGIN,password,access_key_master=master,
            upstream_base_url=self.fake.url,upstream_api_key='synthetic-main-key')
        self.auth={'Authorization':'Bearer '+AccessKeys(self.app,self.owner,master).rotate('one')['key']}
        self.application=create_app(self.settings,image_models=self.vision)
        self.http=TestClient(self.application,base_url=ORIGIN);self.http.__enter__();self.addCleanup(self.http.__exit__,None,None,None)
    def tearDown(self):pass
    def post(self,value=None,*,stream=False,operation='new',request_id=None,model='synthetic-main'):
        request=value or body(1);request=dict(request,model=model,stream=stream,memory=dict(operation=operation))
        if request_id:request['memory']['request_id']=request_id
        return self.http.post('/v1/chat/completions',headers=self.auth,json=request)
    def drain(self):self.http.portal.call(self.application.state.proxy.drain)
    def row(self):
        with connect(self.app) as c:return c.execute("SELECT * FROM memory_core.conversation_messages WHERE bot_id='one' AND role='user' ORDER BY id LIMIT 1").fetchone()
    def test_slow_caption_does_not_delay_reply_pending_falls_back_and_final_prefix_stays_stable(self):
        self.vision.release.clear()
        response=self.post();self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(self.vision.started.wait(2));self.assertIsNone(self.row()['image_descriptions'])
        original=self.fake.requests[0]['body']['messages'][-1]['content']
        self.assertEqual(next(p for p in original if p['type']=='image_url')['image_url']['url'],image_url())
        next_message=dict(model='synthetic-main',messages=[dict(role='user',content='接着聊。')])
        self.assertEqual(self.post(next_message).status_code,200)
        self.assertIn(IMAGE_PLACEHOLDER,str(self.fake.requests[-1]['body']['messages']))
        self.vision.release.set();self.drain()
        self.assertEqual(self.row()['image_descriptions'][0]['description'],'合成图像的客观描述。')
        self.assertEqual(self.post(next_message).status_code,200);third=self.fake.requests[-1]['body']['messages'][:2]
        self.assertIn('[图片：合成图像的客观描述。]',str(third))
        self.assertEqual(self.post(next_message).status_code,200);fourth=self.fake.requests[-1]['body']['messages'][:2]
        serialize=lambda messages:json.dumps(messages,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        self.assertEqual(serialize(third),serialize(fourth))
    def test_streaming_reply_also_finishes_before_caption(self):
        self.vision.release.clear()
        response=self.post(stream=True);self.assertEqual(response.status_code,200);self.assertIn('[DONE]',response.text)
        self.assertTrue(self.vision.started.wait(2));self.assertIsNone(self.row()['image_descriptions'])
        self.vision.release.set();self.drain();self.assertIsNotNone(self.row()['image_descriptions'])
    def test_duplicate_request_and_tool_continuation_do_not_describe_again(self):
        request=body(1);request['model']='synthetic-round'
        first=self.post(request,model='synthetic-round',request_id='images-step-0');self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(self.post(request,model='synthetic-round',request_id='images-step-0').status_code,409)
        self.drain();self.assertEqual(len(self.vision.calls),1)
        message=first.json()['choices'][0]['message'];request['messages']+=[message,dict(role='tool',tool_call_id=message['tool_calls'][0]['id'],content='合成工具结果。')]
        second=self.post(request,operation=None,model='synthetic-round',request_id='images-step-1');self.assertEqual(second.status_code,200,second.text)
        self.drain();self.assertEqual(len(self.vision.calls),1)
    def test_non_chat_and_frontend_old_images_are_not_described(self):
        self.assertEqual(self.post(operation='non_chat').status_code,200);self.drain();self.assertEqual(self.vision.calls,[])
        request=body(1);request['messages']+=[dict(role='assistant',content='前端的旧回答。'),dict(role='user',content='本轮没有图片。')]
        self.assertEqual(self.post(request).status_code,200);self.drain();self.assertEqual(self.vision.calls,[])
        with connect(self.app) as c:self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.conversation_turns').fetchone()['n'],1)
    def test_capacity_and_concurrency_are_bounded_and_stop_cancels_unsaved_images(self):
        manager=self.application.state.proxy.images
        manager.settings=replace(self.settings,image_description_max_pending=1,image_description_concurrency=2)
        self.vision.release.clear()
        self.assertEqual(self.post(body(3)).status_code,200);self.assertTrue(self.vision.started.wait(2))
        self.assertEqual(self.post(body(1)).status_code,200)
        self.assertEqual(len(manager.tasks),1);self.assertLessEqual(self.vision.peak,2)
        self.http.portal.call(manager.stop);self.assertEqual(manager.tasks,{})
        with connect(self.app) as c:
            self.assertTrue(all(r['image_descriptions'] is None for r in c.execute("SELECT image_descriptions FROM memory_core.conversation_messages WHERE role='user'")))
    def test_multi_image_batch_saves_once_in_original_order(self):
        self.assertEqual(self.post(body(3)).status_code,200);self.drain()
        self.assertEqual(len(self.row()['image_descriptions']),3)
        self.assertEqual([r[0]['url'] for r in self.vision.calls],[image_url(100),image_url(101),image_url(102)])
        self.assertNotIn('data:image',str(self.row()))
    def test_finished_first_image_is_not_written_before_the_other_images_finish(self):
        first_finished=threading.Event();self.vision.release.clear()
        original=self.vision.describe
        async def staggered(image,prompt):
            if image['url']==image_url(100):
                first_finished.set();return result('第一张先完成。')
            return await original(image,prompt)
        self.vision.describe=staggered
        self.assertEqual(self.post(body(2)).status_code,200)
        self.assertTrue(first_finished.wait(2));self.assertIsNone(self.row()['image_descriptions'])
        self.vision.release.set();self.drain()
        self.assertEqual([r['description'] for r in self.row()['image_descriptions']],['第一张先完成。','合成图像的客观描述。'])

    def test_image_count_limit_keeps_original_forwarding_and_skips_caption(self):
        self.application.state.proxy.images.settings=replace(self.settings,image_description_max_images=1)
        self.assertEqual(self.post(body(2)).status_code,200);self.drain()
        self.assertEqual(self.vision.calls,[]);self.assertIsNone(self.row()['image_descriptions'])
        sent=self.fake.requests[-1]['body']['messages'][-1]['content']
        self.assertEqual(sum(p['type']=='image_url' for p in sent),2)

    def test_unexpected_background_error_never_contains_key_or_interrupts_reply(self):
        async def broken(image,prompt):raise RuntimeError('synthetic-main-key')
        self.vision.describe=broken
        with self.assertLogs('memory_web.images',level='WARNING') as logs:
            self.assertEqual(self.post(body(2)).status_code,200);self.drain()
        self.assertNotIn('synthetic-main-key',str(logs.output));self.assertIsNone(self.row()['image_descriptions'])

    def test_main_error_is_not_replaced_with_description_and_user_image_still_gets_caption(self):
        response=self.post(model='synthetic-error');self.assertEqual(response.status_code,429,response.text)
        self.drain();self.assertIsNotNone(self.row()['image_descriptions'])
        self.assertNotIn('合成图像的客观描述',response.text)
    def test_explicit_default_http_role_uses_only_fake_provider_and_inherits_main_credentials(self):
        server=ProviderServer();self.addCleanup(server.close)
        seen=[]
        def respond(path,payload):
            seen.append(payload)
            if payload['model']=='synthetic-vision':return 200,completion('默认 HTTP 岗位生成的合成描述。')
            return 200,completion('主模型合成回复。')
        server.hook=respond
        settings=replace(self.settings,upstream_base_url=server.url,image_description_model='synthetic-vision')
        with TestClient(create_app(settings),base_url=ORIGIN) as http:
            response=http.post('/v1/chat/completions',headers=self.auth,json=body(1));self.assertEqual(response.status_code,200,response.text)
            http.portal.call(http.app.state.proxy.drain)
        self.assertEqual([p['model'] for p in seen].count('synthetic-vision'),1)
        self.assertTrue(all(r['authorization']=='Bearer synthetic-main-key' for r in server.requests))
        self.assertIn('默认 HTTP 岗位生成',str(self.row()['image_descriptions']))
        self.assertNotIn('synthetic-main-key',str(self.row()))
    def test_pure_image_still_skips_recall_and_tool_result_images_are_not_captioned(self):
        pure=dict(model='synthetic-main',messages=[dict(role='user',content=[dict(type='image_url',image_url=dict(url=image_url()))])])
        self.assertEqual(self.post(pure).status_code,200);self.drain()
        with connect(self.app) as c:
            turn=c.execute("SELECT recall_snapshot FROM memory_core.conversation_turns WHERE bot_id='one' ORDER BY id LIMIT 1").fetchone()
        self.assertEqual(turn['recall_snapshot']['status'],'skipped_image')
        before=len(self.vision.calls)
        request=dict(model='synthetic-round',messages=[dict(role='user',content='请查询。')])
        first=self.post(request,model='synthetic-round');self.assertEqual(first.status_code,200)
        message=first.json()['choices'][0]['message']
        request['messages'] += [message,dict(role='tool',tool_call_id=message['tool_calls'][0]['id'],content=pure['messages'][0]['content'])]
        self.assertEqual(self.post(request,model='synthetic-round',operation=None).status_code,200);self.drain()
        self.assertEqual(len(self.vision.calls),before)

    def test_final_caption_is_visible_in_readonly_conversation_records(self):
        self.assertEqual(self.post().status_code,200);self.drain()
        login=self.http.post('/api/login',headers={'Origin':ORIGIN},json={'password':'synthetic admin'})
        self.assertEqual(login.status_code,200)
        data=self.http.get('/api/spaces/one/conversations').json()
        message=next(m for m in data['items'][0]['messages'] if m['role']=='user')
        self.assertIn('[图片：合成图像的客观描述。]',message['display_text'])
        self.assertTrue(any(p['type']=='image_placeholder' for p in message['content_parts']))
        self.assertNotIn('image_descriptions',message)
        self.assertEqual(self.post(dict(model='synthetic-main',messages=[dict(role='user',content='接着聊。')])).status_code,200)
        self.drain()
        from memory_core.daily.summaries import SummaryStore
        from memory_core.daily.summary_policy import SummarySettings
        summaries=SummaryStore(self.app,'one',SummarySettings(trigger_messages=4,retain_turns=1))
        day=data['items'][0]['business_date']
        plan=summaries.next_plan(day)
        sid=summaries.save(plan,dict(content='合成的图片摘要。',model='synthetic-summary'))
        detail=self.http.get('/api/spaces/one/summaries/'+str(sid)).json()
        self.assertIn('[图片：合成图像的客观描述。]',detail['turns'][0]['user_display_text'])


    def test_unconfigured_image_role_is_skipped(self):
        with TestClient(create_app(self.settings),base_url=ORIGIN) as http:
            response=http.post('/v1/chat/completions',headers=self.auth,json=body(1));self.assertEqual(response.status_code,200)
            http.portal.call(http.app.state.proxy.drain)
        self.assertIsNone(self.row()['image_descriptions'])
