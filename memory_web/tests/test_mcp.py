"""MCP protocol and real Postgres behavior, with synthetic data/roles only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from psycopg.types.json import Jsonb

from memory_core.cards.cards import CardStore
from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.recall.memory_query import MemoryQueryStore
from memory_core.models.model_config import ModelRole, RecallModelsConfig
from memory_core.storage.postgres import KeywordToken
from memory_core.recall.recall import RecallService
from memory_core.recall.recall_config import RecallSettings
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase
from memory_core.testing.provider_server import ProviderServer
from memory_web.admin.access_keys import AccessKeys
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.fake_upstream import FakeUpstream
from memory_web.testing.recall_models import SyntheticRecallModels

ORIGIN = 'https://memory.example.test'


class MCPTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner, Settings(3))
        install_web(self.owner)
        self.config = SpaceConfiguration(self.owner)
        self.config.configure('one', Participant('小禾', ('禾禾',)), Participant('林舟'))
        self.config.configure('two', Participant('另一用户'), Participant('另一助手'))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        password = Path(temporary.name) / 'password.hash'
        write_password(password, 'synthetic admin')
        master = Fernet.generate_key().decode()
        self.keys = AccessKeys(self.app, self.owner, master)
        self.tokens = {space: self.keys.rotate(space)['key'] for space in ('one', 'two')}
        self.fake = FakeUpstream()
        self.addCleanup(self.fake.close)
        self.models = SyntheticRecallModels()
        self.settings = WebSettings(self.app, self.owner, ORIGIN, password, access_key_master=master,
                                    upstream_base_url=self.fake.url, upstream_api_key='synthetic-upstream')
        self.application = create_app(self.settings, recall_models=self.models)
        self.http = TestClient(self.application, base_url=ORIGIN, follow_redirects=False)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.store = MemoryQueryStore(self.app, 'one')
        self.topic = self.store.topics()[0]['id']

    def tearDown(self):
        pass

    def auth(self, space='one'):
        return {'Authorization': 'Bearer ' + self.tokens[space]}

    def rpc(self, method, params=None, *, space='one', path='/mcp', headers=None):
        return self.http.post(path, headers={**self.auth(space), 'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-06-18', **(headers or {})},
            json=dict(jsonrpc='2.0', id=1, method=method, params=params or {}))

    def call(self, action='browse', *, space='one', **args):
        response = self.rpc('tools/call', dict(name='memory', arguments=dict(action=action, **args)), space=space)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['result']

    def card(self, title='合成卡片', *, space='one', kind='atomic', **changes):
        store = self.store if space == 'one' else MemoryQueryStore(self.app, space)
        mid = store.write_card(title + '的完整正文', [1, 0, 0], [KeywordToken('合成')], title=title,
                               summary=title + '的摘要', source_date='2026-10-01', item_kind=kind)
        if changes:
            from psycopg import sql
            assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in changes)
            with connect(self.app) as c:
                c.execute(sql.SQL('UPDATE memory_core.memory_items SET {} WHERE id=%s').format(assignments),
                          [*changes.values(), mid])
        return mid

    def usage(self, mid):
        with connect(self.app) as c:
            return c.execute('SELECT used_count,last_used_at,restored_at,archive_reason,archived_at,updated_at FROM memory_core.memory_items WHERE id=%s', (mid,)).fetchone()

    def test_official_client_initialize_list_call_and_no_session(self):
        self.card()

        async def verify():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application), headers=self.auth()) as client:
                async with streamable_http_client(ORIGIN + '/mcp', http_client=client) as (read, write, session_id):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.serverInfo.name, 'amber-heart')
                        listed = await session.list_tools()
                        self.assertEqual([tool.name for tool in listed.tools], ['memory', 'mark_moment'])
                        result = await session.call_tool('memory', {'action': 'browse'})
                        self.assertFalse(result.isError)
                        self.assertIn('合成卡片', result.content[0].text)
                        self.assertIsNone(session_id())

        self.http.portal.call(verify)
        response = self.rpc('initialize', dict(protocolVersion='2025-06-18', capabilities={}, clientInfo={'name': 'synthetic', 'version': '1'}))
        self.assertNotIn('mcp-session-id', response.headers)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.rpc('tools/list', path='/mcp/').status_code, 200)

    def test_only_bearer_header_no_url_key_or_admin_session(self):
        body = dict(jsonrpc='2.0', id=1, method='tools/list')
        for path in ('/mcp', '/mcp?key=' + self.tokens['one'], '/mcp?access_token=' + self.tokens['one']):
            response = self.http.post(path, json=body)
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(response.headers['www-authenticate'], 'Bearer')
        login = self.http.post('/api/login', headers={'Origin': ORIGIN}, json={'password': 'synthetic admin'})
        self.assertEqual(login.status_code, 200)
        for method in ('GET', 'POST', 'DELETE'):
            self.assertEqual(self.http.request(method, '/mcp', json=body).status_code, 401)
        for token in ('Basic synthetic', 'Bearer invalid', 'Bearer ' + self.tokens['one'] + ' '):
            self.assertEqual(self.rpc('tools/list', headers={'Authorization': token}).status_code, 401)
        self.assertEqual(self.rpc('tools/list', headers={'Origin': 'https://other.example.test'}).status_code, 403)
        self.keys.revoke('one')
        self.assertEqual(self.rpc('tools/list').status_code, 401)
        with TestClient(create_app(WebSettings(self.app, self.owner, ORIGIN, self.settings.password_file)), base_url=ORIGIN) as unconfigured:
            self.assertEqual(unconfigured.post('/mcp', headers=self.auth(), json=body).status_code, 503)

    def test_space_isolation_dynamic_names_and_strict_arguments(self):
        one = self.card('第一空间卡片')
        self.card('第二空间卡片', space='two')
        for space, own, other in [('one', '小禾', '另一用户'), ('two', '另一用户', '小禾')]:
            description = self.rpc('tools/list', space=space).json()['result']['tools'][0]['description']
            self.assertIn(own, description)
            self.assertNotIn(other, description)
            self.assertNotIn('园丁', description)
            self.assertIn('查到的记忆同样只供参考：', description)
            self.assertIn('除了客观事实与明确的待办约定', description)
        with ThreadPoolExecutor(max_workers=4) as pool:
            spaces = ['one', 'two'] * 6
            descriptions = list(pool.map(lambda space: self.rpc('tools/list', space=space).json()['result']['tools'][0]['description'], spaces))
        for space, description in zip(spaces, descriptions):
            self.assertIn('小禾' if space == 'one' else '另一用户', description)
            self.assertNotIn('另一用户' if space == 'one' else '小禾', description)
        self.config.configure('one', Participant('新用户名'), Participant('新助手名'), expected_revision=1)
        description = self.rpc('tools/list').json()['result']['tools'][0]['description']
        self.assertIn('新用户名', description)
        self.assertNotIn('小禾', description)
        self.assertTrue(self.call('get', space='two', memory_id=f'M-{one}')['isError'])
        self.assertEqual(self.usage(one)['used_count'], 0)
        self.assertNotIn('第一空间卡片', str(self.call(space='two')))
        for args in [dict(action='sql'), dict(action='browse', bot_id='two'), dict(action='browse', limit=True),
                     dict(action='browse', cursor=-1), dict(action='browse', include_outdated='false'),
                     dict(action='get', memory_id='1'), dict(action='get', memory_id='M-2147483648'),
                     dict(action='search'), dict(action='search', query='合成', limit=21),
                     dict(action='get_sources', memory_id=f'M-{one}', limit=31),
                     dict(action='browse', date_from='2026-02-30'),
                     dict(action='browse', date_from='2026-11-01', date_to='2026-10-01'),
                     dict(action='list_topics')]:
            with self.subTest(args=args):
                result = self.call(**args)
                self.assertTrue(result['isError'], result)

    def test_browse_literal_filters_pagination_topics_entities_and_outdated(self):
        entity = self.store.create_entity('合成地点', aliases=['地方别名'])
        ids = [self.card('字面 %_ 原词' + str(i)) for i in range(3)]
        for mid in ids:
            self.store.link_topic(mid, self.topic)
            self.store.link_entity(mid, entity)
        outdated = self.card('字面 %_ 原词旧版', cognitive_status='outdated')
        self.store.link_topic(outdated, self.topic)
        self.card('总结卡片', kind='topic_summary')
        name = self.store.topics()[0]['name']
        page = self.call(text='%_', topic=name, entity='地方别名', limit=2)['structuredContent']
        self.assertEqual([r['memory_id'] for r in page['items']], [f'M-{mid}' for mid in ids[:2]])
        self.assertEqual(page['total_count'], 3)
        tail = self.call(text='%_', cursor=page['next_cursor'], limit=2)['structuredContent']
        self.assertEqual(tail['items'][0]['memory_id'], f'M-{ids[-1]}')
        all_rows = self.call(text='%_', include_outdated=True, order='desc')['structuredContent']['items']
        self.assertEqual(all_rows[0]['memory_id'], f'M-{outdated}')
        self.assertEqual(self.call('list_topics', source_scope='atomic')['structuredContent']['topics'], [name])
        self.assertEqual(self.call('list_topics', source_scope='topic_summary')['structuredContent']['topics'], [])
        self.assertTrue(self.call(topic='不存在的主题')['isError'])
        self.assertTrue(self.call(entity='不存在的实体')['isError'])
        self.assertTrue(all(self.usage(mid)['used_count'] == 0 for mid in ids))

    def test_get_usage_restore_full_text_and_concurrent_counts(self):
        mid = self.card(archive_reason='decay', archived_at='2020-01-01T00:00:00+08:00')
        original = self.usage(mid)
        self.call()
        self.call('search', query='合成')
        self.assertEqual(self.usage(mid), original)
        result = self.call('get', memory_id=f'M-{mid}')
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['items'][0]['content'], '合成卡片的完整正文')
        after = self.usage(mid)
        self.assertEqual(after['used_count'], 1)
        self.assertIsNone(after['archive_reason'])
        self.assertIsNone(after['archived_at'])
        self.assertEqual(after['restored_at'], after['last_used_at'])
        self.assertEqual(after['updated_at'], original['updated_at'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.call('get', memory_id=f'M-{mid}'), range(8)))
        self.assertTrue(all(not r['isError'] for r in results))
        self.assertEqual(self.usage(mid)['used_count'], 9)
        self.assertEqual(self.usage(mid)['restored_at'], after['restored_at'])
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.conversation_requests').fetchone()['n'], 0)
        CardStore(self.app, 'one').delete(mid)
        self.assertTrue(self.call('get', memory_id=f'M-{mid}')['isError'])
        self.assertEqual(self.call()['structuredContent']['items'], [])

    def test_search_three_routes_document_order_fallback_no_selector(self):
        ids = [self.card('合成事件' + str(i)) for i in range(12)]
        entity = self.store.create_entity('合成', embedding=[1, 0, 0])
        for mid in ids:
            self.store.link_entity(mid, entity)
        queried = self.store.search('合成', {}, self.models, RecallSettings())
        self.assertTrue(all(count > 0 for count in queried['retrieval_stats']['candidate_counts'].values()))
        result = self.call('search', query='合成', limit=20)
        calls = {name for name, _ in self.models.calls}
        self.assertIn('embed', calls)
        self.assertIn('rerank', calls)
        self.assertNotIn('select', calls)
        rerank_ids = next(value for name, value in self.models.calls if name == 'rerank')
        self.assertEqual([r['memory_id'] for r in result['structuredContent']['items']], [f'M-{mid}' for mid in reversed(rerank_ids)])
        self.models.fail = {'rerank'}
        fallback = self.call('search', query='合成', limit=20)
        self.assertIn('已退回三路粗检顺序', fallback['content'][0]['text'])
        self.assertEqual([r['memory_id'] for r in fallback['structuredContent']['items']], [f'M-{mid}' for mid in rerank_ids])
        with patch.object(self.models, 'rerank', return_value=[0]):
            incomplete = self.call('search', query='合成', limit=20)
        self.assertEqual(incomplete['structuredContent'], fallback['structuredContent'])
        self.assertTrue(all(self.usage(mid)['used_count'] == 0 for mid in ids))

    def test_default_factory_uses_configured_http_roles_without_llm_selection(self):
        self.card()
        with ProviderServer() as provider:
            settings = replace(self.settings, upstream_base_url=provider.url, recall_models=RecallModelsConfig(
                embedding=ModelRole('synthetic-embedding'), cleaner=ModelRole('synthetic-keywords'),
                reranker=ModelRole('synthetic-document', provider.url, 'synthetic-rerank-key'),
                selector=ModelRole('synthetic-selector')))
            with TestClient(create_app(settings), base_url=ORIGIN) as http:
                response = http.post('/mcp', headers={**self.auth(), 'Accept': 'application/json, text/event-stream'},
                    json=dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(name='memory', arguments=dict(action='search', query='合成'))))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()['result']['isError'])
                self.assertIn('合成卡片', response.text)
            paths = [request['path'] for request in provider.requests]
            self.assertIn('/v1/embeddings', paths)
            self.assertIn('/v1/rerank', paths)
            self.assertFalse(any(request['body'].get('model') == 'synthetic-selector' for request in provider.requests))

    def test_card_deleted_while_document_rerank_is_waiting_is_not_returned(self):
        mid = self.card('精排中删除的卡片')

        def delete_during_rerank(query, candidate_ids, *, documents=None):
            CardStore(self.app, 'one').delete(mid)
            return list(range(len(candidate_ids)))

        with patch.object(self.models, 'rerank', side_effect=delete_during_rerank):
            result = self.call('search', query='合成')
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['items'], [])
        self.assertNotIn('精排中删除的卡片', str(result))

    def test_errors_do_not_expose_database_or_provider_details(self):
        secret = 'synthetic-database-password-never-return'
        with patch.object(MemoryQueryStore, 'policy', side_effect=RuntimeError(secret)), self.assertLogs('memory_web.mcp') as logs:
            response = self.rpc('tools/list')
            self.assertIn('读取记忆空间配置暂时失败', response.text)
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, str(logs.output))
        with patch.object(MemoryQueryStore, 'browse', side_effect=RuntimeError(secret)), self.assertLogs('memory_web.mcp') as logs:
            result = self.call()
            self.assertTrue(result['isError'])
            self.assertNotIn(secret, str(result))
            self.assertNotIn(secret, str(logs.output))

    def test_summary_indexes_sources_deleted_cards_and_archive_scope(self):
        parent = self.card('合成主题总结', kind='topic_summary')
        children = [self.card('合成子卡' + str(i), parent_reflection_id=parent) for i in range(3)]
        with connect(self.app) as c:
            c.execute('UPDATE memory_core.memory_items SET child_episode_ids=%s WHERE id=%s', (Jsonb(children[:2]), parent))
        CardStore(self.app, 'one').delete(children[1])
        search_result = self.call('search', query='合成')
        search = search_result['structuredContent']['items']
        child_refs = {f'M-{children[0]}', f'M-{children[2]}'}
        self.assertEqual({r['memory_id'] for r in search}, child_refs)
        self.assertEqual(len(search), 2)
        self.assertTrue(all(r['parent_memory_id'] == f'M-{parent}' for r in search))
        self.assertEqual(search_result['content'][0]['text'].count(f'所属主题总结：M-{parent}'), 2)
        atomic = self.call('search', query='合成', source_scope='atomic')['structuredContent']['items']
        self.assertEqual(atomic, search)
        summaries = self.call('search', query='合成', source_scope='topic_summary')['structuredContent']['items']
        self.assertEqual([r['memory_id'] for r in summaries], [f'M-{parent}'])
        all_cards = self.call('search', query='合成', source_scope='all')['structuredContent']['items']
        self.assertEqual({r['memory_id'] for r in all_cards}, child_refs | {f'M-{parent}'})
        self.assertEqual(len(all_cards), 3)

        def automatic_recall():
            return RecallService(self.store).recall('合成', embedder=self.models, cleaner=self.models,
                reranker=self.models, selector=self.models, rerank=False)['final']

        self.assertEqual([r['memory_id'] for r in automatic_recall()], [parent])
        sources = self.call('get_sources', memory_id=f'M-{parent}', limit=1)['structuredContent']
        self.assertEqual(sources['total_count'], 2)
        self.assertEqual(sources['items'][0]['memory_id'], f'M-{children[0]}')
        tail = self.call('get_sources', memory_id=f'M-{parent}', cursor=sources['next_cursor'])['structuredContent']
        self.assertEqual(tail['items'][0]['memory_id'], f'M-{children[2]}')
        self.assertTrue(all(self.usage(mid)['used_count'] == 0 for mid in (parent, children[0], children[2])))
        self.call('get', memory_id=f'M-{children[0]}')
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT parent_reflection_id FROM memory_core.memory_items WHERE id=%s', (children[0],)).fetchone()['parent_reflection_id'], parent)
            c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at='2020-01-01T00:00:00+08:00' WHERE id=%s", (parent,))
        self.assertEqual({r['memory_id'] for r in self.call('search', query='合成')['structuredContent']['items']}, child_refs)
        self.assertEqual(automatic_recall(), [])
        self.assertEqual(self.call('search', query='合成', source_scope='topic_summary')['structuredContent']['items'][0]['memory_id'], f'M-{parent}')
        self.call('get_sources', memory_id=f'M-{parent}')
        self.assertIsNotNone(self.usage(parent)['archive_reason'])
        self.assertEqual(self.usage(parent)['used_count'], 0)
        self.call('get', memory_id=f'M-{parent}')
        self.assertIsNone(self.usage(parent)['archive_reason'])

    def test_tool_continuation_keeps_recall_snapshot_and_mcp_avoids_space_lock(self):
        mid = self.card()
        messages = [{'role': 'user', 'content': '查合成旧事'}]
        body = dict(model='synthetic-round', messages=messages)
        first = self.http.post('/v1/chat/completions', headers=self.auth(), json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.http.portal.call(self.application.state.proxy.drain)
        frozen = deepcopy(self.fake.requests[-1]['body']['messages'])
        with connect(self.app) as c:
            before = c.execute('SELECT count(*) AS n FROM memory_core.conversation_requests').fetchone()['n']
            c.execute("SELECT * FROM memory_core.conversation_space_state WHERE bot_id='one' FOR UPDATE")
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                # Holding the exact proxy row lock proves there is no indirect
                # receipt/statistics path in the MCP query.
                result = pool.submit(self.call, 'get', memory_id=f'M-{mid}').result(timeout=3)
                self.assertFalse(result['isError'], result)
            finally:
                c.rollback()
                pool.shutdown()
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.conversation_requests').fetchone()['n'], before)
        calls = len(self.models.calls)
        messages += [first.json()['choices'][0]['message'], {'role': 'tool', 'tool_call_id': 'call-0', 'content': result['content'][0]['text']}]
        second = self.http.post('/v1/chat/completions', headers=self.auth(), json=body)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(frozen, self.fake.requests[-1]['body']['messages'][:-2])
        self.assertEqual(len(self.models.calls), calls)

    def test_our_tool_cards_persist_into_next_round_but_not_body_material(self):
        from memory_core.conversations.conversations import ConversationStore
        from memory_core.conversations.conversation_material import dialogue_input, extraction_segments
        from memory_core.storage.space import read_policy
        from memory_core.recall.tool_history import render_tool_card
        from memory_web.testing.tool_cards import events

        mid = self.card('合成复查约定', is_milestone=True)
        parent = self.card('合成复查主题总结', kind='topic_summary')
        with connect(self.app) as c:
            c.execute('UPDATE memory_core.memory_items SET parent_reflection_id=%s WHERE id=%s', (parent,mid))
            c.execute('UPDATE memory_core.memory_items SET child_episode_ids=%s WHERE id=%s', (Jsonb([mid]),parent))
        queries = [dict(action='search',query='合成复查',limit=1),
                   dict(action='get',memory_id=f'M-{mid}'),
                   dict(action='get_sources',memory_id=f'M-{parent}')]
        texts = [self.call(**args)['content'][0]['text'] for args in queries]
        bookmark_args = dict(action='add',section='health',content='2026年11月10日 已约好下周复诊。')
        marked = self.rpc('tools/call',dict(name='mark_moment',arguments=bookmark_args)).json()['result']
        texts.append(marked['content'][0]['text'])
        aborted_args = dict(action='add', section='longterm', content='周末一起整理旅行照片')
        saved = self.rpc('tools/call', dict(name='mark_moment', arguments=aborted_args)).json()['result']
        self.assertIn('已记下【长期事项】', saved['content'][0]['text'])
        texts.append('McpError: connection abort')
        args = queries + [dict(tool_name='amber-heart:mark_moment',params=bookmark_args), aborted_args]
        names = ['memory','amber_heart__memory_server_1','amber-heart:memory','package_proxy','mark_moment']
        calls, results = [], []
        for index,(name,arguments,body) in enumerate(zip(names,args,texts)):
            call,result = events(name,arguments,body,identifier='our-'+str(index))
            calls.append(call)
            results.append(result)
        store = ConversationStore(self.app,'one')
        body = dict(model='synthetic',messages=[dict(role='user',content='请查询并记下复诊安排。')])
        record = store.begin(body,dict(operation='new'))
        protocol_calls = [dict(id=c['tool_call_id'],type='function',function=c['payload']) for c in calls]
        store.finish(record,content='我查一下再记下来。',tools=protocol_calls,finish_reason='tool_calls')
        body['messages'] += [dict(role='assistant',content='我查一下再记下来。',tool_calls=protocol_calls)]
        body['messages'] += [dict(role='tool',tool_call_id=r['tool_call_id'],content=r['payload']['content']) for r in results]
        step = store.begin(body,{})
        store.finish(step,content='已核对复诊安排。',finish_reason='stop')
        response = self.http.post('/v1/chat/completions',headers=self.auth(),json=dict(model='synthetic',
            messages=[dict(role='user',content='继续刚才的话题。')]))
        self.assertEqual(response.status_code, 200, response.text)
        self.http.portal.call(self.application.state.proxy.drain)
        messages = self.fake.requests[-1]['body']['messages']
        history = messages[-1]['content']
        self.assertEqual(messages[-1]['role'], 'user')
        self.assertTrue(history.startswith('【系统记录】\n'))
        self.assertTrue(history.endswith('继续刚才的话题。'))
        self.assertEqual(messages[-3]['role'], 'user')
        self.assertIn('历史中的工具记录仅供参考，需要使用工具时直接调用', messages[-3]['content'])
        self.assertNotIn('【调用工具】', str(messages[:-1]))
        for call,result in zip(calls,results):
            card = render_tool_card(call,result)
            self.assertIsNotNone(card)
            # The persisted event payload, not a second query, builds this card.
            self.assertIn(card,history)
        self.assertIn('合成复查约定的摘要',history)
        self.assertIn('合成复查约定的完整正文',history)
        self.assertIn('【检索边界】相关候选，不是完整清单',history)
        self.assertIn('【结果摘要】已记下【健康关注】',history)
        self.assertIn(
            '【调用工具】mark_moment({"action": "add", "section": "longterm", "content": "周末一起整理旅行照片"})\n'
            '【结果未知】收到回传，但无法确认工具执行结果。',
            history,
        )
        self.assertNotIn('connection abort', history)
        self.assertNotIn('完整结果在 tool_trace',history)
        self.assertEqual(self.usage(mid)['used_count'],1)
        with connect(self.app) as c:
            turns = c.execute("SELECT * FROM memory_core.conversation_turns WHERE bot_id='one' ORDER BY position_id").fetchall()
            policy = read_policy(c,'one')
            dialogue,_,_ = dialogue_input(c,'one',turns,policy,store.settings)
            segments,_,_ = extraction_segments(c,'one',turns,store.settings)
        self.assertNotIn('【调用工具】',dialogue)
        self.assertNotIn('合成复查约定的完整正文',dialogue)
        self.assertNotIn('【结果摘要】',str(segments))
        self.assertNotIn('周末一起整理旅行照片', dialogue)
        self.assertNotIn('周末一起整理旅行照片', str(segments))
        self.assertIn('已核对复诊安排。',dialogue)
        self.assertIn('继续刚才的话题。',dialogue)
        # Another space never receives this ledger or any of its cards.
        self.http.post('/v1/chat/completions',headers=self.auth('two'),json=dict(model='synthetic',
            messages=[dict(role='user',content='另一空间的合成对话。')]))
        self.assertNotIn('【调用工具】memory',str(self.fake.requests[-1]['body']['messages']))

    def test_public_renderers_generate_reproducible_card_samples(self):
        from memory_web.testing.tool_cards import samples
        examples = samples()
        self.assertEqual(set(examples),{'search','get','get_sources','mark_moment_add'})
        for label,sample in examples.items():
            self.assertIsNotNone(sample['card'],label)
            self.assertNotIn('tool_trace',sample['card'])
        self.assertIn('【读取范围】已审核记忆卡片正文',examples['get']['card'])
        self.assertIn('里程碑',examples['get_sources']['card'])
