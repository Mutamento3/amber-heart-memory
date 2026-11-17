"""Reproducible synthetic 5D cards using the actual MCP and history renderers."""

import json

from memory_core.tool_history import render_tool_card
from memory_web.mcp_tools import render_result
from memory_web.bookmark_tool import render_bookmarks


def events(name, args, text, identifier='synthetic-call'):
    call = dict(kind='call', tool_call_id=identifier, tool_name=name,
                payload=dict(name=name, arguments=json.dumps(args, ensure_ascii=False)))
    result = dict(kind='result', tool_call_id=identifier,
                  payload=dict(content=[dict(type='text', text=text)]))
    return call, result


def samples():
    row = dict(id=101, title='复查后的用药安排', source_date='2026-11-08',
        topics=['身体与生活'], item_kind='atomic', cognitive_status='stable', archive_reason=None,
        parent_reflection_id=None, experience_mode='external', is_milestone=False,
        summary='小禾复查后继续按医嘱用药，约好两周后复诊。',
        content='2026年11月8日，小禾复查后继续按医嘱用药。林舟答应陪她核对复诊安排，两周后再复诊。')
    parent = dict(row, id=202, title='最近的复查与生活安排', item_kind='topic_summary')
    source = dict(row, parent_reflection_id=202, is_milestone=True)
    values = [
        ('search', dict(action='search', query='上次复查后怎么安排用药？', limit=1),
         dict(items=[row], total_count=2, cursor=0, next_cursor=1, ordering_stats={}, retrieval_stats={})),
        ('get', dict(action='get', memory_id='M-101'), dict(item=row)),
        ('get_sources', dict(action='get_sources', memory_id='M-202'),
         dict(parent=parent, items=[source], total_count=1, cursor=0, next_cursor=None)),
    ]
    output = {}
    for label, args, value in values:
        body, _ = render_result(args['action'], value)
        call, result = events('memory', args, body)
        output[label] = dict(call=call, result=result, card=render_tool_card(call, result))
    args = dict(action='add', section='health', content='2026年11月9日 已约好两周后复诊。')
    value = dict(ok=True, id='a1b2c3d4', section='health', content=args['content'])
    body = render_bookmarks('add', value)
    call, result = events('mark_moment', args, body)
    output['mark_moment_add'] = dict(call=call, result=result, card=render_tool_card(call, result))
    return output


if __name__ == '__main__':
    for label, sample in samples().items():
        print(label + '\n' + sample['card'] + '\n')
