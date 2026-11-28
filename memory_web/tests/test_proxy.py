from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hmac
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import psycopg
from psycopg import sql
import uvicorn

from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.storage.space import SpaceConfiguration, Participant
from memory_core.tests.support import DatabaseTestCase
from memory_web.admin.access_keys import AccessKeys, InvalidAccessKey, KeyConfigurationError
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.install import install_web, fingerprint, MIGRATION
from memory_web.admin.password import write_password
from memory_web.testing.fake_upstream import FakeUpstream


ORIGIN = 'https://memory.example.test'
PASSWORD = 'synthetic admin password'
UPSTREAM_KEY = 'synthetic-upstream-secret'


def tool_call(identifier, name='weather', arguments='{}'):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


class ProxyTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner, Settings(3))
        install_web(self.owner)
        for space in ('one', 'two'):
            SpaceConfiguration(self.owner).configure(space, Participant('用户'), Participant('助手'))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.password = Path(temporary.name) / 'password.hash'
        write_password(self.password, PASSWORD)
        self.master = Fernet.generate_key().decode()
        self.keys = AccessKeys(self.app, self.owner, self.master)
        self.token = self.keys.rotate('one')['key']
        self.fake = FakeUpstream()
        self.addCleanup(self.fake.close)
        self.settings = WebSettings(self.app, self.owner, ORIGIN, self.password,
            access_key_master=self.master, upstream_base_url=self.fake.url, upstream_api_key=UPSTREAM_KEY)
        self.application = create_app(self.settings)
        self.http = TestClient(self.application, base_url=ORIGIN, follow_redirects=False)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)

    def tearDown(self):
        pass

    @property
    def auth(self):
        return {'Authorization': 'Bearer ' + self.token}

    def body(self, model='synthetic-model', **extra):
        return {'model': model, 'messages': [{'role': 'user', 'content': '合成问句'}], **extra}

    def chat(self, body=None):
        response = self.http.post('/v1/chat/completions', headers=self.auth, json=body or self.body())
        self.http.portal.call(self.application.state.proxy.drain)
        return response

    def rows(self, table):
        with connect(self.app) as c:
            return c.execute(sql.SQL('SELECT * FROM memory_core.{} ORDER BY id').format(sql.Identifier(table))).fetchall()

    def test_keys_lookup_decrypt_once_constant_compare_rotation_and_scopes(self):
        second = self.keys.rotate('two')['key']
        with patch.object(self.keys.cipher, 'decrypt', wraps=self.keys.cipher.decrypt) as decrypt, patch('memory_web.admin.access_keys.hmac.compare_digest', wraps=hmac.compare_digest) as compare:
            self.assertEqual(self.keys.authenticate(self.token), 'one')
            self.assertEqual(decrypt.call_count, 1)
            self.assertEqual(compare.call_count, 1)
        with self.assertRaises(InvalidAccessKey):
            self.keys.authenticate(self.token[:-1] + ('A' if self.token[-1] != 'A' else 'B'))
        with connect(self.app) as c:
            rows = c.execute('SELECT * FROM memory_web.space_access_keys').fetchall()
        self.assertNotIn(self.token, str(rows))
        self.assertNotIn(self.master, str(rows))
        self.assertEqual(AccessKeys(self.app, self.owner, self.master).get('one')['key'], self.token)
        with self.assertRaises(KeyConfigurationError):
            AccessKeys(self.app, self.owner, Fernet.generate_key().decode()).get('one')
        self.assertEqual(self.http.get('/api/spaces', headers=self.auth).status_code, 401)
        replacement = self.keys.rotate('one')['key']
        with self.assertRaises(InvalidAccessKey):
            self.keys.authenticate(self.token)
        self.assertEqual(self.keys.authenticate(replacement), 'one')
        self.assertEqual(self.keys.authenticate(second), 'two')
        self.keys.revoke('one')
        with self.assertRaises(InvalidAccessKey):
            self.keys.authenticate(replacement)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with connect(self.app) as c:
                c.execute('UPDATE memory_web.space_access_keys SET revoked_at=NULL')

    def test_concurrent_key_rotation_leaves_exactly_one_valid_key(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.keys.rotate('one'), range(2)))
        with connect(self.app) as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM memory_web.space_access_keys WHERE bot_id='one' AND revoked_at IS NULL").fetchone()['n'], 1)
        self.assertEqual(self.keys.authenticate(self.keys.get('one')['key']), 'one')

    def test_key_admin_endpoints_require_cookie_origin_csrf_and_support_revisit(self):
        path = '/api/spaces/one/access-key'
        self.assertEqual(self.http.get(path, headers=self.auth).status_code, 401)
        login = self.http.post('/api/login', headers={'Origin': ORIGIN}, json={'password': PASSWORD})
        csrf = login.json()['csrf']
        self.assertEqual(self.http.post(path, headers={'Origin': ORIGIN}, json={}).status_code, 403)
        value = self.http.get(path)
        self.assertEqual(value.json()['key'], self.token)
        self.assertEqual(value.headers['cache-control'], 'no-store')
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf}
        new = self.http.post(path, headers=headers, json={}).json()['key']
        self.assertNotEqual(new, self.token)
        self.assertEqual(self.http.get(path).json()['key'], new)
        self.assertEqual(self.http.delete(path, headers=headers).status_code, 415)
        self.assertEqual(self.http.request('DELETE', path, headers=headers, json={}).status_code, 200)
        self.assertIsNone(self.http.get(path).json()['key'])

    def test_models_nonstream_default_configuration_and_no_history_duplication(self):
        with patch.dict('os.environ', {'MEMORY_DSN': self.app, 'MEMORY_CONFIG_DSN': self.owner,
            'MEMORY_WEB_ORIGIN': ORIGIN, 'MEMORY_WEB_PASSWORD_FILE': str(self.password),
            'MEMORY_ACCESS_KEY_MASTER_KEY': self.master, 'MEMORY_UPSTREAM_BASE_URL': self.fake.url,
            'MEMORY_UPSTREAM_API_KEY': UPSTREAM_KEY}, clear=True):
            default = create_app()
            with TestClient(default, base_url=ORIGIN) as client:
                self.assertEqual(client.get('/v1/models', headers=self.auth).json()['data'][0]['id'], 'synthetic-model')
        body = self.body()
        body['messages'] = [{'role': 'system', 'content': '前端设置的系统词'},
                            {'role': 'user', 'content': '旧历史'}, {'role': 'assistant', 'content': '旧回复'}, *body['messages']]
        self.assertEqual(self.chat(body).json()['choices'][0]['message']['content'], '合成回答')
        sent = self.fake.requests[-1]
        self.assertEqual(sent['body']['messages'][0], body['messages'][0])
        self.assertNotIn('旧历史', str(sent['body']))
        self.assertNotIn('旧回复', str(sent['body']))
        self.assertTrue(sent['body']['messages'][-1]['content'].endswith('合成问句'))
        self.assertEqual(sent['authorization'], 'Bearer ' + UPSTREAM_KEY)
        self.assertNotIn(self.token, str(sent))
        messages = self.rows('conversation_messages')
        self.assertEqual(len(messages), 2)
        self.assertNotIn('旧历史', str(messages))
        self.assertNotIn('不应存档', str(messages))
        request = self.rows('conversation_requests')[0]
        self.assertEqual(request['usage']['total_tokens'], 13)
        self.assertEqual(request['state'], 'complete')

    def test_auxiliary_request_never_touches_conversation_storage(self):
        with patch('memory_web.proxy.proxy.ConversationStore', side_effect=AssertionError('must not archive')):
            response = self.chat(self.body(memory={'operation': 'non_chat'}, stream=True))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('memory', self.fake.requests[-1]['body'])
        for name in ('turns', 'requests', 'messages', 'tool_events'):
            self.assertEqual(self.rows('conversation_' + name), [])
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.conversation_space_state').fetchone()['n'], 0)

    def test_upstream_failure_has_user_only_and_no_keys_in_errors(self):
        response = self.chat(self.body('synthetic-error'))
        self.assertEqual(response.status_code, 429)
        self.assertNotIn(UPSTREAM_KEY, response.text)
        self.assertNotIn(self.token, response.text)
        self.assertEqual([m['role'] for m in self.rows('conversation_messages')], ['user'])
        self.assertEqual(self.rows('conversation_requests')[0]['state'], 'upstream_failed')

    def test_user_write_failure_prevents_any_upstream_call(self):
        with patch.object(ConversationStore, 'begin', side_effect=psycopg.OperationalError('synthetic DB unavailable')):
            response = self.chat()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.rows('conversation_turns'), [])

    def test_archive_failure_records_safe_ids_and_failure_state_without_secrets(self):
        original = ConversationStore.finish
        attempts = []

        def fail_once(store, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise psycopg.OperationalError(UPSTREAM_KEY + self.token)
            return original(store, *args, **kwargs)

        with patch.object(ConversationStore, 'finish', fail_once), self.assertLogs('memory_web.proxy', level='ERROR') as logs:
            response = self.chat(self.body(stream=True))
        self.assertEqual(response.status_code, 200)
        self.assertIn('第二段', response.text)
        self.assertNotIn(self.token, str(logs.output))
        self.assertNotIn(UPSTREAM_KEY, str(logs.output))
        self.assertIn('request_id=', str(logs.output))
        self.assertEqual(self.rows('conversation_requests')[0]['state'], 'archive_failed')
        self.assertEqual(len(self.rows('conversation_messages')), 1)

    def test_stream_unicode_heartbeat_usage_tools_and_safe_error(self):
        response = self.chat(self.body('synthetic-tools', stream=True))
        self.assertEqual(response.status_code, 200)
        self.assertIn('第一段', response.text)
        self.assertIn('不应存档的合成思考', response.text)
        self.assertEqual(self.rows('conversation_messages')[1]['content_parts'][0]['text'], '第一段')
        self.assertNotIn('不应存档', str(self.rows('conversation_messages')))
        tool = self.rows('conversation_tool_events')[0]
        self.assertEqual(tool['payload'], {'name': 'weather', 'arguments': '{"city":"test"}'})
        self.assertEqual(self.rows('conversation_requests')[0]['usage'], {'total_tokens': 13})
        ConversationStore.expire_leases(self.app, now=datetime.now(timezone.utc)+timedelta(seconds=301))
        response = self.chat(self.body('synthetic-stream-error', stream=True))
        self.assertNotIn(UPSTREAM_KEY, response.text)
        self.assertEqual(self.rows('conversation_requests')[-1]['state'], 'upstream_failed')
        self.assertEqual(len(self.rows('conversation_messages')), 3)
        response = self.chat(self.body('synthetic-truncated', stream=True))
        self.assertIn('stream_read_failed', response.text)
        self.assertEqual(self.rows('conversation_requests')[-1]['state'], 'interrupted')

    def test_images_forward_but_only_placeholder_is_archived(self):
        body = self.body()
        body['messages'][0]['content'] = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,SYNTHETIC_IMAGE'}}]
        self.assertEqual(self.chat(body).status_code, 200)
        self.assertEqual(self.fake.requests[-1]['body']['messages'][-1]['content'][-1], body['messages'][0]['content'][0])
        self.assertEqual(self.rows('conversation_messages')[0]['content_parts'], [{'type': 'image_placeholder'}])
        self.assertNotIn('SYNTHETIC_IMAGE', str(self.rows('conversation_turns')))

    def test_unsupported_parts_in_old_messages_are_dropped_but_named_in_this_round(self):
        body = self.body()
        video = {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,SYNTHETIC_VIDEO'}}
        body['messages'] = [
            {'role': 'user', 'content': [{'type': 'text', 'text': '合成旧问句'}, video]},
            {'role': 'assistant', 'content': '合成旧回答'},
            {'role': 'user', 'content': '合成问句'},
        ]
        self.assertEqual(self.chat(body).status_code, 200)
        self.assertNotIn('SYNTHETIC_VIDEO', json.dumps(self.fake.requests[-1]['body']))
        body = self.body()
        body['messages'][0]['content'] = [{'type': 'text', 'text': '合成问句'}, video]
        response = self.chat(body)
        self.assertEqual(response.status_code, 400)
        self.assertIn('video_url', response.text)

    def test_unsolicited_tool_tail_is_rejected_before_upstream(self):
        body = self.body()
        body['messages'] += [{'role': 'tool', 'tool_call_id': 'call-unknown', 'content': '合成结果'}]
        self.assertEqual(self.chat(body).status_code, 409)
        self.assertEqual(self.fake.requests, [])

    def test_explicit_request_ids_and_cross_space_targets(self):
        body = self.body(memory={'request_id': 'same-request', 'operation': 'new'})
        self.assertEqual(self.chat(body).status_code, 200)
        self.assertEqual(self.chat(body).status_code, 409)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.rows('conversation_requests')[0]['client_operation'], 'new')
        other = ConversationStore(self.app, 'two').begin(self.body(), {})
        self.assertEqual(self.chat(self.body(memory={'operation': 'edit', 'target_turn_id': other['turn_id']})).status_code, 409)
        self.assertEqual(len(self.fake.requests), 1)

    def test_stream_reaches_socket_before_upstream_finishes_and_slow_archive(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        origin = 'http://127.0.0.1:' + str(port)
        app = create_app(replace(self.settings, origin=origin, local_http=True))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', proxy_headers=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        archive_started, archive_release = threading.Event(), threading.Event()
        original = ConversationStore.finish

        def slow_archive(store, *args, **kwargs):
            archive_started.set()
            archive_release.wait(5)
            return original(store, *args, **kwargs)

        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.01)
            self.assertTrue(server.started)
            with patch.object(ConversationStore, 'finish', slow_archive):
                with httpx.stream('POST', origin + '/v1/chat/completions', headers=self.auth,
                                  json=self.body('synthetic-slow', stream=True), timeout=5) as response:
                    lines = response.iter_lines()
                    for line in lines:
                        if '第一段' in line:
                            break
                    else:
                        self.fail('first content never arrived')
                    self.assertFalse(self.fake.release.is_set())
                    self.fake.release.set()
                    remainder = '\n'.join(lines)
                    self.assertIn('[DONE]', remainder)
                    self.assertIn('第二段', remainder)
                    self.assertTrue(archive_started.wait(2))
                    self.assertFalse(archive_release.is_set())
                archive_release.set()
        finally:
            self.fake.release.set()
            archive_release.set()
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
        self.assertFalse(thread.is_alive())

        self.assertEqual(self.rows('conversation_requests')[0]['state'], 'complete')

    def test_web_v1_upgrade_preserves_sessions_and_repeats(self):
        import hashlib
        with connect(self.owner) as c:
            c.execute('DROP SCHEMA memory_web CASCADE')
            c.execute(MIGRATION.read_text())
            c.execute('GRANT USAGE ON SCHEMA memory_web TO memory_app')
            c.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA memory_web TO memory_app')
            c.execute("INSERT INTO memory_web.admin_login_attempts VALUES ('synthetic',1,'2030-01-01T00:00:00+08:00')")
            record = {'version': 1, 'migration': hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
                      'catalog': fingerprint(c), 'application_role': 'memory_app'}
            c.execute(sql.SQL('COMMENT ON SCHEMA memory_web IS {}').format(sql.Literal(json.dumps(record))))
        self.assertTrue(install_web(self.owner))
        self.assertFalse(install_web(self.owner))
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_web.admin_login_attempts').fetchone()['n'], 1)

    def test_real_socket_disconnect_saves_received_text_as_incomplete(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        origin = 'http://127.0.0.1:' + str(sock.getsockname()[1])
        app = create_app(replace(self.settings, origin=origin, local_http=True))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', proxy_headers=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.01)
            self.assertTrue(server.started)
            with httpx.stream('POST', origin + '/v1/chat/completions', headers=self.auth,
                              json=self.body('synthetic-slow', stream=True), timeout=5) as response:
                for line in response.iter_lines():
                    if '第一段' in line:
                        break
                else:
                    self.fail('no content before disconnect')
            for _ in range(100):
                rows = self.rows('conversation_requests')
                if rows and rows[0]['state'] == 'interrupted':
                    break
                time.sleep(0.02)
            self.assertEqual(rows[0]['state'], 'interrupted')
            messages = self.rows('conversation_messages')
            self.assertEqual(messages[-1]['content_parts'][0]['text'], '第一段')
            self.assertEqual(messages[-1]['content_state'], 'incomplete')
        finally:
            self.fake.release.set()
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
        self.assertFalse(thread.is_alive())


    def test_regenerate_edit_and_failure_keep_current_version_until_success(self):
        initial = self.body('synthetic-echo', memory={'request_id': 'initial'})
        first = self.chat(initial)
        first_text = first.json()['choices'][0]['message']['content']
        second_body = self.body('synthetic-echo')
        second_body['messages'] = [initial['messages'][0], {'role': 'assistant', 'content': first_text}, {'role': 'user', 'content': '第二句'}]
        second = self.chat(second_body)
        second_id = int(second.headers['x-memory-turn-id'])
        regenerated = self.chat(second_body)
        regenerated_id = int(regenerated.headers['x-memory-turn-id'])
        turns = {t['id']: t for t in self.rows('conversation_turns')}
        self.assertEqual(turns[second_id]['history_state'], 'replaced')
        self.assertEqual(turns[regenerated_id]['position_id'], turns[second_id]['position_id'])
        second_body['messages'][-1]['content'] = '第二句改写'
        edited = self.chat(second_body)
        edited_id = int(edited.headers['x-memory-turn-id'])
        second_body['model'] = 'synthetic-error'
        failure = self.chat(second_body)
        self.assertEqual(failure.status_code, 429)
        turns = {t['id']: t for t in self.rows('conversation_turns')}
        self.assertEqual(turns[edited_id]['history_state'], 'current')
        self.assertEqual(turns[max(turns)]['history_state'], 'candidate')
        self.assertIn('合成额度不足', failure.text)

    def test_tool_steps_share_turn_and_frozen_context_across_app_instances(self):
        from copy import deepcopy
        initial = self.body('synthetic-round')
        initial['messages'].insert(0, {'role': 'system', 'content': '初始系统词'})
        first = self.chat(initial)
        self.assertEqual(first.status_code, 200, first.text)
        turn_id = int(first.headers['x-memory-turn-id'])
        frozen_time = self.rows('conversation_turns')[0]['frozen_context']['frozen_at']
        continuation = deepcopy(initial)
        continuation['messages'][0]['content'] = '后来更改的系统词'
        message = first.json()['choices'][0]['message']
        continuation['messages'] += [message, {'role': 'tool', 'tool_call_id': message['tool_calls'][0]['id'], 'content': '第一次工具结果'}]
        continuation['memory'] = {'request_id': 'step1'}
        # A fresh worker must use the persisted turn, not a process-local map.
        with TestClient(create_app(self.settings), base_url=ORIGIN) as another:
            second = another.post('/v1/chat/completions', headers=self.auth, json=continuation)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(int(second.headers['x-memory-turn-id']), turn_id)
        self.assertEqual(self.fake.requests[-1]['body']['messages'][0]['content'], '初始系统词')
        self.assertEqual(self.rows('conversation_turns')[0]['frozen_context']['frozen_at'], frozen_time)
        message = second.json()['choices'][0]['message']
        continuation['messages'] += [message, {'role': 'tool', 'tool_call_id': message['tool_calls'][0]['id'], 'content': '第二次工具结果'}]
        continuation['memory'] = {'request_id': 'step2'}
        continuation['stream'] = True
        third = self.chat(continuation)
        self.assertEqual(third.status_code, 200, third.text)
        self.assertIn('合成步骤2', third.text)
        self.assertEqual([r['step_index'] for r in self.rows('conversation_requests')], [0, 1, 2])
        self.assertEqual(len(self.rows('conversation_turns')), 1)
        self.assertEqual(sum(m['role']=='user' for m in self.rows('conversation_messages')), 1)
        self.assertIsNone(self.rows('conversation_turns')[0]['frozen_context'])
        before = len(self.fake.requests)
        duplicate = self.chat(continuation)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()['error']['state'], 'complete')
        self.assertEqual(len(self.fake.requests), before)

    def test_kelivo_blank_assistant_and_reasoning_echo_are_forwarded(self):
        # Kelivo ad141129, chat_completions_api.dart:_buildAssistantToolCallMessage:
        # an empty tool-only assistant gets "\n\n" plus reasoning replay fields.
        self.fake.tool_batches = [[tool_call('upstream-kelivo', 'memory')]]
        self.fake.reply = ''
        initial = self.body('synthetic-round')
        first = self.chat(initial)
        self.assertEqual(first.status_code, 200, first.text)
        prefix = self.fake.requests[-1]['body']['messages'][:-1]
        message = first.json()['choices'][0]['message']
        self.assertEqual(message['content'], '')
        message.update(content='\n\n', reasoning_content='合成思考回传',
                       reasoning_details=[{'type': 'reasoning.text', 'text': '合成协议附加信息'}])
        tail = [message, {'role': 'tool', 'tool_call_id': 'upstream-kelivo', 'name': 'memory', 'content': '合成查档结果'}]
        self.fake.reply = '合成续轮回复'
        second = self.chat(dict(initial, messages=initial['messages'] + tail))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.headers['x-memory-turn-id'], first.headers['x-memory-turn-id'])
        sent = self.fake.requests[-1]['body']['messages']
        self.assertEqual(sent[:-3], prefix)
        self.assertEqual(sent[-2:], tail)
        self.assertNotIn('合成思考回传', str(self.rows('conversation_messages')))
        self.assertNotIn('reasoning_details', str(self.rows('conversation_tool_events')))

    def test_operit_regenerated_ids_map_to_ledger_and_allow_compact_next_step(self):
        # Operit 16cffb98 OpenAIProvider/StructuredToolCallBridge regenerates
        # nine-character IDs and omits content when the tool-only text is blank.
        self.fake.tool_batches = [[tool_call('upstream-memory', 'package_proxy',
            '{"tool_name":"amber-heart:memory","params":{"action":"search"}}')],
            [tool_call('upstream-bookmark', 'package_proxy',
            '{"tool_name":"amber-heart:mark_moment","params":{"action":"list"}}')]]
        self.fake.reply = ''
        initial = self.body('synthetic-round')
        first = self.chat(initial)
        self.assertEqual(first.status_code, 200, first.text)
        client_call = first.json()['choices'][0]['message']['tool_calls'][0]
        client_call['id'] = 'daxkrp0nx'
        # Re-serializing arguments is harmless as well.
        client_call['function']['arguments'] = json.dumps(json.loads(client_call['function']['arguments']), ensure_ascii=False)
        tail = [{'role': 'assistant', 'tool_calls': [client_call]},
                {'role': 'tool', 'tool_call_id': 'daxkrp0nx', 'content': '合成查档结果'}]
        second = self.chat(dict(initial, messages=initial['messages'] + tail))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.fake.requests[-1]['body']['messages'][-2:], tail)
        self.assertEqual(self.rows('conversation_tool_events')[1]['tool_call_id'], 'upstream-memory')
        self.fake.reply = '合成最终回复'
        compact = {'model': 'synthetic-round', 'messages': [
            {'role': 'tool', 'tool_call_id': 'upstream-bookmark', 'content': '合成书签结果'}]}
        third = self.chat(compact)
        self.assertEqual(third.status_code, 200, third.text)
        self.assertEqual(third.headers['x-memory-turn-id'], first.headers['x-memory-turn-id'])
        sent = self.fake.requests[-1]['body']['messages']
        self.assertEqual([m['tool_call_id'] for m in sent if m['role'] == 'tool'], ['upstream-memory', 'upstream-bookmark'])
        self.assertEqual([t['id'] for m in sent for t in m.get('tool_calls', [])], ['upstream-memory', 'upstream-bookmark'])
        self.assertEqual(len(self.rows('conversation_requests')), 3)
        self.assertEqual(sum(m['role'] == 'user' for m in self.rows('conversation_messages')), 1)

    def test_two_parallel_tools_keep_client_pairing_and_original_ledger_ids(self):
        self.fake.tool_batches = [[tool_call('original-memory', 'memory'), tool_call('original-bookmark', 'mark_moment')]]
        initial = self.body('synthetic-round')
        first = self.chat(initial)
        self.assertEqual(first.status_code, 200, first.text)
        calls = first.json()['choices'][0]['message']['tool_calls']
        calls[0]['id'], calls[1]['id'] = 'daxkrp0nx', 'daxkro1nx'
        # Results can finish in reverse order; their client IDs still decide
        # which result belongs to each position in the original call batch.
        tail = [{'role': 'assistant', 'tool_calls': calls},
            {'role': 'tool', 'tool_call_id': 'daxkro1nx', 'content': '合成书签结果'},
            {'role': 'tool', 'tool_call_id': 'daxkrp0nx', 'content': '合成记忆结果'}]
        second = self.chat(dict(initial, messages=initial['messages'] + tail))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.fake.requests[-1]['body']['messages'][-3:], tail)
        events = self.rows('conversation_tool_events')
        self.assertEqual([e['tool_call_id'] for e in events],
                         ['original-memory', 'original-bookmark', 'original-bookmark', 'original-memory'])
        self.assertEqual([e['payload']['content'][0]['text'] for e in events[2:]], ['合成书签结果', '合成记忆结果'])

    def test_invalid_renamed_batches_do_not_write_or_reach_upstream(self):
        self.fake.tool_batches = [[tool_call('a', 'memory'), tool_call('b', 'mark_moment')]]
        initial = self.body('synthetic-round')
        self.assertEqual(self.chat(initial).status_code, 200)
        assistant = {'role': 'assistant', 'tool_calls': [tool_call('x', 'memory'), tool_call('y', 'mark_moment')]}
        def result(identifier):
            return {'role': 'tool', 'tool_call_id': identifier, 'content': '合成结果'}
        cases = [
            [assistant, result('x')],
            [assistant, result('x'), result('x')],
            [assistant, result('a'), result('y')],
            [{'role': 'assistant', 'tool_calls': [tool_call('x', 'mark_moment'), tool_call('y', 'memory')]}, result('x'), result('y')],
        ]
        for tail in cases:
            with self.subTest(tail=tail):
                response = self.chat(dict(initial, messages=initial['messages'] + tail))
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()['error']['code'], 'tool_results_mismatch')
                self.assertEqual(len(self.fake.requests), 1)
                self.assertEqual(len(self.rows('conversation_requests')), 1)
                self.assertEqual(len(self.rows('conversation_tool_events')), 2)
        # Failed validation leaves the pending round available for correction.
        corrected = self.chat(dict(initial, messages=initial['messages'] + [assistant, result('x'), result('y')]))
        self.assertEqual(corrected.status_code, 200, corrected.text)

    def test_streaming_tool_done_commits_waiting_state_before_instant_continuation(self):
        from copy import deepcopy
        initial = self.body('synthetic-round', stream=True)
        # No helper drain between the two HTTP exchanges.
        first = self.http.post('/v1/chat/completions', headers=self.auth, json=initial)
        self.assertEqual(first.status_code, 200)
        continuation = deepcopy(initial)
        continuation['stream'] = False
        continuation['messages'] += [{'role': 'assistant', 'content': '合成步骤0', 'tool_calls': [
            {'id': 'call-0', 'type': 'function', 'function': {'name': 'weather', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call-0', 'content': '瞬间完成'}]
        second = self.http.post('/v1/chat/completions', headers=self.auth, json=continuation)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.headers['x-memory-turn-id'], second.headers['x-memory-turn-id'])

    def test_nonstream_archive_failure_still_returns_generated_reply(self):
        original = ConversationStore.finish
        attempts = []
        def fail_once(store, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise psycopg.OperationalError(UPSTREAM_KEY)
            return original(store, *args, **kwargs)
        with patch.object(ConversationStore, 'finish', fail_once), self.assertLogs('memory_web.proxy', level='ERROR') as logs:
            response = self.chat()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['choices'][0]['message']['content'], '合成回答')
        self.assertEqual(self.rows('conversation_requests')[0]['state'], 'archive_failed')
        self.assertNotIn(UPSTREAM_KEY, str(logs.output))
        with connect(self.app) as c:
            self.assertIsNone(c.execute('SELECT active_turn_id FROM memory_core.conversation_space_state').fetchone()['active_turn_id'])

    def test_master_mismatch_is_clear_503_for_proxy_and_admin(self):
        settings = replace(self.settings, access_key_master=Fernet.generate_key().decode())
        with TestClient(create_app(settings), base_url=ORIGIN) as client:
            response = client.get('/v1/models', headers=self.auth)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['error']['code'], 'key_configuration_error')
            self.assertIn('主密钥', response.text)
            client.post('/api/login', headers={'Origin': ORIGIN}, json={'password': PASSWORD})
            response = client.get('/api/spaces/one/access-key')
            self.assertEqual(response.status_code, 503)
            self.assertIn('主密钥', response.json()['detail'])
            self.assertNotIn(self.token, response.text)
        self.assertEqual(self.fake.requests, [])

    def test_revoked_ciphertext_is_erased_and_upgrade_v2_is_atomic(self):
        import hashlib
        from memory_web.install import MIGRATION_2, MIGRATION_3
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with connect(self.owner) as c:
            c.execute('DROP SCHEMA memory_web CASCADE')
            c.execute(MIGRATION.read_text())
            c.execute(MIGRATION_2.read_text())
            c.execute('GRANT USAGE ON SCHEMA memory_web TO memory_app')
            c.execute('GRANT SELECT ON memory_web.space_access_keys TO memory_app')
            c.execute("INSERT INTO memory_web.space_access_keys VALUES (%s,'one','synthetic old ciphertext','2026-11-01T12:00:00+08:00','2026-11-01T12:01:00+08:00')", ('a'*32,))
            metadata = dict(version=2,migration=hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
                migration_2=hashlib.sha256(MIGRATION_2.read_bytes()).hexdigest(),catalog=fingerprint(c),application_role='memory_app')
            c.execute(sql.SQL('COMMENT ON SCHEMA memory_web IS {}').format(sql.Literal(json.dumps(metadata))))
        with self.assertRaisesRegex(ValueError, '升级作废钥匙'):
            create_app(self.settings)
        fault = Path(temporary.name) / '003.sql'
        fault.write_text(MIGRATION_3.read_text()+'\nSELECT 1/0;')
        with patch('memory_web.install.MIGRATION_3', fault), self.assertRaises(psycopg.errors.DivisionByZero):
            install_web(self.owner)
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT ciphertext FROM memory_web.space_access_keys').fetchone()['ciphertext'], 'synthetic old ciphertext')
        self.assertTrue(install_web(self.owner))
        self.assertFalse(install_web(self.owner))
        with connect(self.app) as c:
            self.assertIsNone(c.execute('SELECT ciphertext FROM memory_web.space_access_keys').fetchone()['ciphertext'])
        current = self.keys.rotate('one')['key']
        self.keys.rotate('one')
        self.keys.revoke('one')
        with connect(self.app) as c:
            self.assertTrue(all(row['ciphertext'] is None for row in c.execute('SELECT ciphertext FROM memory_web.space_access_keys')))
        with self.assertRaises(InvalidAccessKey):
            self.keys.authenticate(current)

    def test_upstream_errors_keep_reason_redact_key_and_are_bounded(self):
        response = self.chat(self.body('synthetic-error'))
        self.assertIn('合成额度不足', response.text)
        self.fake.models_error = '模型列表失败：'+UPSTREAM_KEY+' '+('长错误'*500)
        response = self.http.get('/v1/models', headers=self.auth)
        self.assertIn('模型列表失败', response.text)
        self.assertNotIn(UPSTREAM_KEY,response.text)
        self.assertLessEqual(len(response.json()['error']['message']),601)
        response = self.chat(self.body('synthetic-stream-error',stream=True))
        self.assertIn('合成流式供应商故障',response.text)
        self.assertNotIn(UPSTREAM_KEY,response.text)
        def broken(request):
            raise httpx.ConnectError('合成连接故障：'+UPSTREAM_KEY,request=request)
        self.application.state.proxy.transport=httpx.MockTransport(broken)
        response=self.chat(self.body(memory={'operation':'new'}))
        self.assertEqual(response.status_code,502)
        self.assertIn('合成连接故障',response.text)
        self.assertNotIn(UPSTREAM_KEY,response.text)
        response=self.http.get('/v1/models',headers=self.auth)
        self.assertNotIn(UPSTREAM_KEY,response.text)
        self.assertIn('合成连接故障',response.text)

    def test_two_frontends_get_busy_while_nonchat_bypasses_lease(self):
        def first():
            return self.http.post('/v1/chat/completions',headers=self.auth,json=self.body('synthetic-slow',stream=True))
        with ThreadPoolExecutor(max_workers=1) as pool:
            running=pool.submit(first)
            try:
                for _ in range(100):
                    if self.fake.requests:
                        break
                    time.sleep(0.02)
                self.assertTrue(self.fake.requests)
                busy=self.http.post('/v1/chat/completions',headers=self.auth,json=self.body(memory={'operation':'new'}))
                self.assertEqual(busy.status_code,409,busy.text)
                self.assertEqual(busy.json()['error']['code'],'space_busy')
                auxiliary=self.http.post('/v1/chat/completions',headers=self.auth,json=self.body(memory={'operation':'non_chat'}))
                self.assertEqual(auxiliary.status_code,200)
            finally:
                self.fake.release.set()
            self.assertEqual(running.result(timeout=10).status_code,200)
        self.http.portal.call(self.application.state.proxy.drain)
        self.assertEqual(len(self.rows('conversation_turns')),1)

    def test_waiting_tool_is_automatically_released_after_configured_timeout(self):
        settings=replace(self.settings,turn_timeout_seconds=2)
        with TestClient(create_app(settings),base_url=ORIGIN) as client:
            response=client.post('/v1/chat/completions',headers=self.auth,json=self.body('synthetic-round'))
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(self.rows('conversation_turns')[0]['generation_state'],'waiting_tools')
            for _ in range(250):
                turn=self.rows('conversation_turns')[0]
                if turn['generation_state']=='incomplete':
                    break
                time.sleep(0.02)
            self.assertEqual(turn['generation_state'],'incomplete')
            self.assertIsNone(turn['frozen_context'])
            response=client.post('/v1/chat/completions',headers=self.auth,json=self.body(memory={'operation':'new'}))
            self.assertEqual(response.status_code,200,response.text)



    def test_live_generation_renews_lease_past_tool_wait_timeout(self):
        settings = replace(self.settings, turn_timeout_seconds=2)
        with TestClient(create_app(settings), base_url=ORIGIN) as client:
            with ThreadPoolExecutor(max_workers=1) as pool:
                running = pool.submit(client.post, '/v1/chat/completions', headers=self.auth,
                    json=self.body('synthetic-slow', stream=True))
                try:
                    for _ in range(100):
                        if self.fake.requests:
                            break
                        time.sleep(0.02)
                    self.assertTrue(self.fake.requests)
                    time.sleep(2.3)
                    response = client.post('/v1/chat/completions', headers=self.auth,
                        json=self.body(memory={'operation':'new'}))
                    self.assertEqual(response.json()['error']['code'],'space_busy')
                    self.assertEqual(self.rows('conversation_turns')[0]['generation_state'],'generating')
                finally:
                    self.fake.release.set()
                self.assertEqual(running.result(timeout=10).status_code,200)
        self.assertEqual(self.rows('conversation_requests')[0]['state'],'complete')



    def test_lease_lost_before_upstream_headers_rejects_late_response(self):
        import asyncio
        async def late(request):
            await asyncio.sleep(0.8)
            return httpx.Response(200, json={'choices':[{'index':0,'message':{'role':'assistant','content':'迟到输出'},'finish_reason':'stop'}]}, request=request)
        service = self.application.state.proxy
        service.settings = replace(self.settings, turn_timeout_seconds=1)
        service.transport = httpx.MockTransport(late)
        with patch.object(ConversationStore, 'renew', return_value=False):
            response = self.chat()
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['error']['code'],'turn_timeout')
        self.assertEqual(self.rows('conversation_requests')[0]['state'],'interrupted')
        self.assertEqual(len(self.rows('conversation_messages')),1)



    def test_unanswered_third_party_resend_edit_and_followup_without_memory(self):
        from copy import deepcopy
        for index, replacement_text in enumerate(('失败输入', '改写后的失败输入')):
            with self.subTest(replacement_text=replacement_text):
                initial = self.body('synthetic-echo', messages=[{'role':'user','content':'接续基准'+str(index)}])
                initial_reply = self.chat(initial).json()['choices'][0]['message']['content']
                failed_body = self.body('synthetic-error', messages=[
                    {'role':'assistant','content':initial_reply}, {'role':'user','content':'失败输入'}])
                failed_response = self.chat(failed_body)
                self.assertEqual(failed_response.status_code,429)
                failed_id = int(failed_response.headers['x-memory-turn-id'])
                replacement = deepcopy(failed_body)
                replacement['model'] = 'synthetic-echo'
                replacement['messages'][-1]['content'] = replacement_text
                self.assertNotIn('memory', replacement)
                reply = self.chat(replacement)
                self.assertEqual(reply.status_code,200,reply.text)
                replacement_id = int(reply.headers['x-memory-turn-id'])
                turns = {t['id']:t for t in self.rows('conversation_turns')}
                self.assertEqual(turns[replacement_id]['replaces_turn_id'],failed_id)
                self.assertEqual(turns[replacement_id]['position_id'],turns[failed_id]['position_id'])
                self.assertEqual(turns[failed_id]['history_state'],'replaced')
                self.assertEqual(turns[replacement_id]['history_state'],'current')
                operation = next(r['resolved_operation'] for r in self.rows('conversation_requests') if r['turn_id']==replacement_id)
                self.assertEqual(operation,'regenerate' if replacement_text=='失败输入' else 'edit')
        initial_reply = self.chat(self.body('synthetic-echo',messages=[{'role':'user','content':'继续聊天的基准'}])).json()['choices'][0]['message']['content']
        failed_body = self.body('synthetic-error',messages=[{'role':'assistant','content':initial_reply},{'role':'user','content':'失败后继续'}])
        failed_response = self.chat(failed_body)
        failed_id = int(failed_response.headers['x-memory-turn-id'])
        followup = deepcopy(failed_body)
        followup['model'] = 'synthetic-echo'
        followup['messages'].append({'role':'user','content':'直接接着说的新话'})
        response = self.chat(followup)
        self.assertEqual(response.status_code,200,response.text)
        new_id = int(response.headers['x-memory-turn-id'])
        turns = {t['id']:t for t in self.rows('conversation_turns')}
        self.assertIsNone(turns[new_id]['replaces_turn_id'])
        self.assertEqual(turns[failed_id]['history_state'],'current')

    def test_third_party_regeneration_after_visible_partial_reply(self):
        initial_reply = self.chat(self.body('synthetic-echo')).json()['choices'][0]['message']['content']
        request = self.body('synthetic-truncated',stream=True,messages=[
            {'role':'assistant','content':initial_reply},{'role':'user','content':'收到半段再重试'}])
        partial = self.chat(request)
        partial_id = int(partial.headers['x-memory-turn-id'])
        self.assertEqual(self.rows('conversation_requests')[-1]['state'],'interrupted')
        self.assertEqual(self.rows('conversation_messages')[-1]['content_parts'][0]['text'],'第一段')
        request['model'] = 'synthetic-echo'
        request['stream'] = False
        completed = self.chat(request)
        self.assertEqual(completed.status_code,200,completed.text)
        new_id = int(completed.headers['x-memory-turn-id'])
        turns = {t['id']:t for t in self.rows('conversation_turns')}
        self.assertEqual(turns[new_id]['replaces_turn_id'],partial_id)
        self.assertEqual(turns[partial_id]['history_state'],'replaced')


class ProxyConfigurationTests(DatabaseTestCase):
    def test_startup_requires_conversation_schema_and_key_table(self):
        install(self.owner, Settings(3), target_version=9)
        install_web(self.owner)
        with tempfile.TemporaryDirectory() as directory:
            password = Path(directory) / 'password.hash'
            write_password(password, PASSWORD)
            settings = WebSettings(self.app, self.owner, ORIGIN, password,
                access_key_master=Fernet.generate_key().decode(),
                upstream_base_url='http://127.0.0.1:1/v1', upstream_api_key=UPSTREAM_KEY)
            self.assertNotIn(settings.access_key_master, repr(settings))
            self.assertNotIn(UPSTREAM_KEY, repr(settings))
            with self.assertRaisesRegex(ValueError, "版本 13"):
                create_app(settings)
            install(self.owner, Settings(3))
            with connect(self.owner) as c:
                c.execute('DROP TABLE memory_web.space_access_keys')
            with self.assertRaisesRegex(ValueError, '升级网页结构'):
                create_app(settings)
