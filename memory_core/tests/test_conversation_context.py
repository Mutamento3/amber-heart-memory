from copy import deepcopy
import unittest

from memory_core.storage.config import Settings
from memory_core.conversations.conversation_context import (
    continuation_body,
    freeze_context,
    freeze_message,
    tool_result_pairs,
)


def call(identifier, name):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}


def result(identifier):
    return {'role': 'tool', 'tool_call_id': identifier, 'content': '合成结果'}


class ContinuationTests(unittest.TestCase):
    def test_pair_ids_before_order_and_map_renamed_parallel_calls(self):
        pending = [call('original-a', 'memory'), call('original-b', 'mark_moment')]
        for client_ids in [('original-a', 'original-b'), ('daxkrp0nx', 'daxkro1nx'), ('original-a', 'daxkro1nx')]:
            with self.subTest(client_ids=client_ids):
                messages = [
                    {'role': 'assistant', 'tool_calls': [call(client_ids[0], 'memory'), call(client_ids[1], 'mark_moment')]},
                    result(client_ids[1]), result(client_ids[0]),
                ]
                original = deepcopy(messages)
                pairs = tool_result_pairs(messages, pending)
                self.assertEqual([identifier for identifier, _ in pairs], ['original-b', 'original-a'])
                self.assertEqual([message for _, message in pairs], messages[-2:])
                self.assertEqual(messages, original)
        # A compact continuation needs no echoed calls when its IDs match.
        self.assertEqual(tool_result_pairs([result('original-b'), result('original-a')], pending),
                         [('original-b', result('original-b')), ('original-a', result('original-a'))])

    def test_mismatched_results_cannot_be_assigned_to_pending_calls(self):
        pending = [call('a', 'memory'), call('b', 'mark_moment')]
        assistant = {'role': 'assistant', 'tool_calls': [call('x', 'memory'), call('y', 'mark_moment')]}
        cases = [
            [assistant, result('x')],
            [assistant, result('x'), result('x')],
            [assistant, result('a'), result('y')],
            [assistant, result('x'), result('z')],
            [result('x'), result('y')],
            [{'role': 'assistant', 'tool_calls': [call('x', 'mark_moment'), call('y', 'memory')]}, result('x'), result('y')],
            [{'role': 'assistant', 'tool_calls': [call('x', 'memory'), call('x', 'mark_moment')]}, result('x'), result('y')],
        ]
        for messages in cases:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                tool_result_pairs(messages, pending)

    def test_client_current_round_is_forwarded_without_text_or_argument_comparison(self):
        initial = {'model': 'synthetic', 'messages': [{'role': 'user', 'content': '原本问句'}]}
        frozen = freeze_context(initial, 0, '2026-11-11T01:00:00+08:00')
        client = {'model': 'synthetic', 'messages': [
            {'role': 'user', 'content': '客户端重发的问句', 'name': 'synthetic-user'},
            {'role': 'assistant', 'content': '\n\n', 'reasoning_content': '合成思考',
             'tool_calls': [call('client-id', 'memory')]}, result('client-id')]}
        expected = [{'role': 'assistant', 'content': '原本正文', 'tool_calls': [call('ledger-id', 'memory')]}, result('ledger-id')]
        self.assertEqual(continuation_body(client, frozen, 0, expected, Settings(3))['messages'], client['messages'])
        minimal = {'model': 'synthetic', 'messages': [result('ledger-id')]}
        restored = continuation_body(minimal, frozen, None, expected, Settings(3))
        self.assertEqual(restored['messages'][0], initial['messages'][0])
        self.assertEqual(restored['messages'][1:], expected)

    def test_compact_continuation_replays_current_user_tool_record_once(self):
        initial = {'model': 'synthetic', 'messages': [{'role': 'user', 'content': '接着查'}]}
        frozen = freeze_context(initial, 0, '2026-11-11T01:00:00+08:00')
        frozen['current'] = freeze_message({'role': 'user', 'content': '[11-11 01:00] 接着查'})
        frozen['current_user_history_prefix'] = '【系统记录】\n[本轮调用了工具：memory（未返回）]\n\n'
        expected = [{'role': 'assistant', 'tool_calls': [call('ledger-id', 'memory')]}, result('ledger-id')]
        minimal = {'model': 'synthetic', 'messages': [result('ledger-id')]}
        restored = continuation_body(minimal, frozen, None, expected, Settings(3))
        self.assertEqual(restored['messages'][0], {
            'role': 'user',
            'content': '【系统记录】\n[本轮调用了工具：memory（未返回）]\n\n[11-11 01:00] 接着查',
        })
        self.assertEqual(restored['messages'][-1], expected[-1])
