"""Known client names, bounded history and opaque fallback; no private imports."""

import json
import unittest

from memory_core.storage.config import Settings
from memory_core.conversations.conversation_history import render_turn
from memory_core.recall.tool_history import render_tool_card


def events(name, args, body, identifier='call'):
    return (dict(kind='call', tool_call_id=identifier, tool_name=name,
                 payload=dict(name=name, arguments=json.dumps(args, ensure_ascii=False))),
            dict(kind='result', tool_call_id=identifier,
                 payload=dict(content=[dict(type='text', text=body)])))


def memory_body(action='get', count=1, content='合成正文', metadata='', next_cursor=None):
    title = dict(get='读取记忆全文', search='搜索相关记忆', browse='浏览记忆', get_sources='回查主题总结来源')[action]
    lines = [title]
    if action=='get_sources':
        lines.append('主题总结：M-90 合成总结')
    for i in range(count):
        lines += ['', f'[M-{i+1}] 标题：合成标题{i+1}\n日期：2026-11-08\n主题：合成主题', metadata+content]
    if not count:
        lines.append('没有找到符合条件的记忆。')
    if action!='get':
        lines.append(f'\n本页 {count} 条，共 {count+bool(next_cursor)} 条。')
    if next_cursor is not None:
        lines.append(f'还有下一页，请使用 cursor={next_cursor}。')
    if action=='search':
        lines.append('这是相关候选，不是完整清单；完整盘点请用 browse。')
    lines.append('这些是审核后的记忆卡片，不是原始聊天逐字稿。')
    return '\n'.join(lines)


class ToolHistoryTests(unittest.TestCase):
    def test_direct_kelivo_conflicts_operit_and_proxy_envelopes(self):
        args = dict(action='get', memory_id='M-1')
        names = ['memory', 'amber_heart__memory', 'amber_heart__memory_abcdefgh', 'mcp_123__memory_2',
                 'amber-heart:memory', 'amber-heart__memory_server_1']
        # Kelivo's server ID suffix can contain underscores, within eight chars.
        for name in names:
            with self.subTest(name=name):
                call, result = events(name, args, memory_body())
                self.assertIn('1. M-1｜2026-11-08｜合成标题1', render_tool_card(call,result))
        for proxy in ('package_proxy','proxy'):
            for params in (args, json.dumps(args)):
                call, result = events(proxy, dict(tool_name='amber-heart:memory',params=params),memory_body())
                self.assertIn('【调用工具】memory(',render_tool_card(call,result))

    def test_foreign_names_malformed_calls_and_pending_results_stay_opaque(self):
        for name in ('recall_memory','memory_backup','memory__other','arbitrary-memory', 'amber-heart:memory:other'):
            call,result=events(name,dict(action='get'),memory_body())
            self.assertIsNone(render_tool_card(call,result))
        call,result=events('memory',dict(action='get'),memory_body())
        self.assertIsNone(render_tool_card(call,None))
        for arguments in ('{bad','[]',{'action':[]},'{}'):
            self.assertIsNone(render_tool_card(dict(call,payload={'arguments':arguments}),result))

    def test_unknown_memory_results_keep_only_supported_call_arguments(self):
        call, result = events('memory', dict(action='search', query='上次约好怎么整理照片',
                              topic={'private': 'hidden'}, extra='hidden'), 'unrecognized secret payload')
        for payload in (None, {'content': []}, {'content': [{'type': 'image_placeholder'}]}, result['payload']):
            with self.subTest(payload=payload):
                self.assertEqual(
                    render_tool_card(call, dict(result, payload=payload)),
                    '【调用工具】memory({"action": "search", "query": "上次约好怎么整理照片"})\n'
                    '【结果未知】收到回传，但无法确认工具执行结果。',
                )

    def test_get_and_summary_limits_do_not_depend_on_private_tool_trace(self):
        args=dict(action='get',memory_id='M-1')
        call,result=events('memory',args,memory_body(content='甲'*501))
        card=render_tool_card(call,result)
        self.assertIn('   '+'甲'*500+'…',card)
        self.assertNotIn('甲'*501,card)
        call,result=events('memory',args,memory_body(content='乙'*4001,
            metadata='类型：主题总结，可用 get_sources 回查来源卡片。\n'))
        card=render_tool_card(call,result)
        self.assertIn('主题总结',card)
        self.assertIn('乙'*4000+'…',card)
        self.assertNotIn('乙'*4001,card)
        self.assertNotIn('tool_trace',card)

    def test_search_page_limit_status_markers_sources_and_body_metadata(self):
        call,result=events('memory',dict(action='search',query='合成'),memory_body('search',13,next_cursor=13,
            metadata='历史版本：不能当作当前事实。\n里程碑：是\n'))
        card=render_tool_card(call,result)
        self.assertIn('共14条，本页返回13条；下一页 cursor=13',card)
        self.assertIn('相关候选，不是完整清单',card)
        self.assertIn('12. M-12',card)
        self.assertNotIn('13. M-13',card)
        self.assertIn('里程碑、状态:outdated',card)
        call,result=events('memory',dict(action='get_sources',memory_id='M-90'),memory_body('get_sources',
            metadata='存在争议：回答时保留不确定性。\n里程碑：是\n'))
        card=render_tool_card(call,result)
        self.assertIn('里程碑、存在争议',card)
        self.assertNotIn('合成正文',card)
        call,result=events('memory',dict(action='get'),memory_body(content='经历：这是正文，不能误当元信息删掉。'))
        self.assertIn('   经历：这是正文',render_tool_card(call,result))

    def test_sources_keep_entire_returned_page_without_body_or_twelve_item_notice(self):
        call, result = events('memory', dict(action='get_sources', memory_id='M-90'),
                              memory_body('get_sources', 30, next_cursor=30))
        card = render_tool_card(call, result)
        self.assertIn('共31条，本页返回30条；下一页 cursor=30', card)
        self.assertIn('30. M-30｜2026-11-08｜合成标题30', card)
        self.assertNotIn('31. M-31', card)
        self.assertNotIn('合成正文', card)
        self.assertNotIn('这里只列前12条', card)

    def test_empty_topics_failures_and_invalid_counts(self):
        call,result=events('memory',dict(action='browse'),memory_body('browse',0))
        self.assertIn('共0条，本页返回0条',render_tool_card(call,result))
        call,result=events('memory',dict(action='list_topics',source_scope='all'),'当前记忆池中有内容的主题：甲、乙')
        self.assertIn('【可查询主题｜全部记忆】共2个：甲、乙',render_tool_card(call,result))
        call,result=events('memory',dict(action='get',memory_id='M-404'),'没有找到记忆 M-404，它可能已删除或不属于当前空间')
        self.assertIn('【执行失败】没有找到记忆 M-404',render_tool_card(call,result))
        call,result=events('memory',dict(action='browse'),memory_body('browse').replace('本页 1 条','本页 2 条'))
        self.assertEqual(render_tool_card(call,result),
                         '【调用工具】memory({"action": "browse"})\n【结果未知】收到回传，但无法确认工具执行结果。')

    def test_bookmark_default_add_list_update_delete_and_fallback(self):
        values=[({}, '已记下【健康关注】书签 a1b2c3d4：2026年11月9日 已约复诊', 'add, 全部'),
                ({'action':'list'}, '共 1 条待进快照书签\n[a1b2c3d4]【健康关注】合成进展\n记录时间：2026-11-09', 'list, 全部'),
                ({'action':'list'}, '共 1 条待整理书签\n[a1b2c3d4]【健康关注】合成进展\n记录时间：2026-11-09', 'list, 全部'),
                ({'action':'update','ids':['a1b2c3d4']}, '已改写【长期事项】书签 a1b2c3d4：改写后的全文', 'update, a1b2c3d4'),
                ({'action':'delete','ids':['a1b2c3d4']}, '已撤回 1 条书签','delete, a1b2c3d4')]
        for args,body,label in values:
            call,result=events('amber_heart__mark_moment',args,body)
            card=render_tool_card(call,result)
            self.assertIn('mark_moment('+label+')',card)
            self.assertIn('【结果摘要】',card)
        call, result = events('mark_moment', dict(action='update', section='longterm',
                              content='甲' * 31, ids=['a1b2c3d4', 'invalid private value']), 'McpError: connection abort')
        self.assertEqual(
            render_tool_card(call, result),
            '【调用工具】mark_moment({"action": "update", "section": "longterm", "content": "'
            + '甲' * 30 + '…", "ids": ["a1b2c3d4"]})\n【结果未知】收到回传，但无法确认工具执行结果。',
        )
        call, result = events('mark_moment', {'section': 'longterm'},
                              '工具参数无效，请核对类型、范围和必填项：content')
        self.assertEqual(
            render_tool_card(call, result),
            '【调用工具】mark_moment({"action": "add", "section": "longterm"})\n'
            '【执行失败】工具参数无效，请核对类型、范围和必填项：content',
        )

    def test_call_id_order_fallback_and_unanswered_semantics_are_preserved(self):
        call,result=events('memory',dict(action='get'),memory_body())
        unknown,unknown_result=events('other',{},'must not replay','different')
        tools=[call,unknown,unknown_result,result]
        messages=[dict(role='user',occurred_at='2026-11-09T12:00:00+08:00',content_parts=[dict(type='text',text='合成问题')])]
        rendered=render_turn(dict(generation_state='incomplete'),messages,tools,Settings(3))
        self.assertIn('未获回应',rendered[0]['content'])
        self.assertIn('【调用工具】memory(',rendered[1]['content'])
        self.assertIn('[本轮调用了工具：other（已返回）]',rendered[1]['content'])
        self.assertNotIn('must not replay',str(rendered))
        self.assertNotIn('[这轮回复未完成]',str(rendered))
