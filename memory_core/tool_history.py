"""Readable cards for our two MCP tools; unknown client payloads stay opaque.

This reads only the existing event ledger. It never queries memories or invokes
models, and is deliberately separate from body-only diary/extraction material.
"""

import json
import re

from .recent_note_text import SECTIONS
from . import chat_text

MEMORY_ARGUMENTS = ('action', 'query', 'topic', 'entity', 'text', 'date_from', 'date_to',
                    'include_outdated', 'source_scope', 'order', 'limit', 'cursor', 'memory_id')
ACTIONS = {'search': '搜索相关记忆', 'browse': '浏览记忆', 'get': '读取记忆全文',
           'get_sources': '回查主题总结来源'}
FOOTNOTE = '这些是审核后的记忆卡片，不是原始聊天逐字稿。'
SEARCH_BOUNDARY = '这是相关候选，不是完整清单；完整盘点请用 browse。'
PAGE = re.compile(r'本页 (\d+) 条，共 (\d+) 条。')
NEXT = re.compile(r'还有下一页，请使用 cursor=(\d+)。')
CARD = re.compile(r'^\[(M-[1-9]\d*)\] 标题：([^\n]*)\n日期：([^\n]*)\n主题：([^\n]*)\n', re.M)
# Kelivo qualifies conflicts and may append an eight-character server ID or a
# numeric collision counter. Truncated names that lose the tool name fall back.
KELIVO = re.compile(r'[A-Za-z_][A-Za-z0-9_-]*__(memory|mark_moment)(?:_(?:[A-Za-z0-9_-]{1,8}|\d+))?')
OPERIT = re.compile(r'[^:\r\n]+:(memory|mark_moment)')
ERROR = re.compile(r'^(?:工具参数无效，|工具调用暂时失败|memory_id 必须是|没有找到记忆 M-|'
                   r'get_sources 需要主题总结|日期必须是有效的|date_from 不能晚于|'
                   r'action=(?:search|list_topics) 时|(?:browse|search|get_sources) 的 limit 最多为|'
                   r'(?:主题|实体)「|不支持的查记忆动作|content 不能为空|'
                   r'(?:add|update|delete) (?:必须|的 ids)|ids 只接受|section 必须是|'
                   r'没找到编号 |编号 [0-9a-f]+ 匹配到多条书签)')


def one_line(value, limit=None):
    value = ' '.join(value.split())
    if limit is not None and len(value) > limit:
        return value[:limit].rstrip() + '…'
    return value


def object_value(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('Expected tool arguments object')
    return value


def tool_identity(name, args):
    if name in ('package_proxy', 'proxy'):
        name = args.get('tool_name')
        args = object_value(args.get('params'))
    if name not in ('memory', 'mark_moment'):
        if not isinstance(name, str):
            raise ValueError('Unknown tool name')
        match = OPERIT.fullmatch(name)
        if match is None and len(name) <= 64:
            match = KELIVO.fullmatch(name)
        if match is None:
            raise ValueError('Unknown tool name')
        name = match[1]
    return name, args


def result_text(event):
    parts = event['payload']['content']
    if (not isinstance(parts, list) or not parts
            or any(not isinstance(p, dict) or p.get('type') != 'text'
                   or not isinstance(p.get('text'), str) for p in parts)):
        raise ValueError('Unknown result content')
    return ''.join(part['text'] for part in parts).strip()


def memory_items(action, body):
    lines = body.splitlines()
    if not lines or lines.pop(0) != ACTIONS[action] or not lines or lines.pop() != FOOTNOTE:
        raise ValueError('Unknown memory result')
    if action == 'search':
        if not lines or lines.pop() != SEARCH_BOUNDARY:
            raise ValueError('Unknown search boundary')
        for note in ('部分检索步骤暂不可用，当前结果来自已完成的检索。',
                     '第一级文档精排失败，已退回三路粗检顺序。'):
            if lines and lines[-1] == note:
                lines.pop()
    cursor = None
    if lines and (match := NEXT.fullmatch(lines[-1])):
        cursor = int(match[1])
        lines.pop()
    total = None
    count = 1
    if action != 'get':
        if not lines or not (match := PAGE.fullmatch(lines.pop())):
            raise ValueError('Missing page counts')
        count, total = map(int, match.groups())
    if action == 'get_sources':
        if not lines or not re.fullmatch(r'主题总结：M-[1-9]\d* .+', lines.pop(0)):
            raise ValueError('Missing source parent')
    content = '\n'.join(lines).strip()
    matches = list(CARD.finditer(content))
    if (len(matches) != count or (total is not None and count > total)
            or (matches and matches[0].start() != 0)):
        raise ValueError('Ambiguous memory cards')
    if not matches and content != '没有找到符合条件的记忆。':
        raise ValueError('Missing empty-result marker')
    items = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        detail = content[match.end():end].strip()
        markers = []
        is_summary = False
        status = 'stable'
        # Only metadata at the start of a card is interpreted; matching phrases
        # inside the actual narrative stay narrative text.
        metadata = [
            ('类型：主题总结，可用 get_sources 回查来源卡片。', '主题总结'),
            (None, None),  # Parent ID has a variable value.
            ('状态：已归档，get 打开全文会自动恢复。', '已归档'),
            ('历史版本：不能当作当前事实。', 'outdated'),
            ('存在争议：回答时保留不确定性。', 'disputed'),
            ('里程碑：是', '里程碑'),
        ]
        for expected, marker in metadata:
            line, _, rest = detail.partition('\n')
            if expected is None:
                if not re.fullmatch(r'所属主题总结：M-[1-9]\d*', line):
                    continue
                marker = line
            elif line != expected:
                continue
            if marker == '主题总结':
                is_summary = True
            if marker in ('outdated', 'disputed'):
                status = marker
            else:
                markers.append(marker)
            detail = rest
        line, _, rest = detail.partition('\n')
        for mode, wording in chat_text.EXPERIENCE.items():
            if line == wording:
                markers.append({'shared': '共同赛博', 'dream': '梦', 'imagined': '想象'}[mode])
                detail = rest
                break
        items.append(dict(id=match[1], title=one_line(match[2]), date=one_line(match[3]),
                          markers=markers, status=status, detail=detail, is_summary=is_summary))
    return items, total, cursor


def memory_card(args, body):
    action = args.get('action')
    if action not in (*ACTIONS, 'list_topics'):
        raise ValueError('Unknown memory action')
    visible = {key: args[key] for key in MEMORY_ARGUMENTS if key in args}
    encoded = json.dumps(visible, ensure_ascii=False)
    if len(encoded) > 4000:
        raise ValueError('Oversized call header')
    lines = ['【调用工具】memory(' + encoded + ')']
    if ERROR.match(body):
        lines.append('【结果详情】' + one_line(body, 500))
    elif action == 'list_topics':
        prefix = '当前记忆池中有内容的主题：'
        if not body.startswith(prefix) or '\n' in body:
            raise ValueError('Unknown topics result')
        topics = body[len(prefix):]
        count = 0 if topics == '暂无' else len(topics.split('、'))
        scope = {'atomic': '原子记忆', 'topic_summary': '主题总结', 'all': '全部记忆'}.get(args.get('source_scope'), '记忆')
        lines.append(f'【可查询主题｜{scope}】共{count}个：' + ('' if topics == '暂无' else topics))
    else:
        items, total, cursor = memory_items(action, body)
        if action == 'get':
            lines.append('【读取范围】已审核记忆卡片正文，不是原始聊天逐字稿')
        else:
            lines.append(f'【结果详情】共{total}条，本页返回{len(items)}条' +
                         (f'；下一页 cursor={cursor}' if cursor is not None else ''))
            if action == 'search':
                lines.append('【检索边界】相关候选，不是完整清单；完整盘点应使用 browse')
            elif action == 'get_sources':
                lines.append('【读取范围】主题总结的来源记忆卡片，不是原始聊天逐字稿')
        log_items = items if action == 'get_sources' else items[:12]
        for index, item in enumerate(log_items, 1):
            header = f'{index}. {item["id"]}｜{item["date"]}｜{item["title"]}'
            markers = item['markers'][:]
            if action == 'get_sources':
                if item['status'] != 'stable':
                    markers.append({'outdated': '过时', 'disputed': '存在争议'}[item['status']])
            else:
                markers.append('状态:' + item['status'])
            if markers:
                header += '｜' + '、'.join(markers)
            lines.append(header)
            if action != 'get_sources' and item['detail']:
                lines.append('   ' + one_line(item['detail'], 4000 if item['is_summary'] else 500))
        if action != 'get_sources' and len(items) > 12:
            lines.append('【可读日志】这里只列前12条')
    return '\n'.join(lines)


def bookmark_card(args, body):
    action = args.get('action') or 'add'
    if isinstance(action, str) and not action.strip():
        action = 'add'
    if action not in ('add', 'list', 'update', 'delete'):
        raise ValueError('Unknown bookmark action')
    if action in ('update', 'delete'):
        ids = args.get('ids')
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
            raise ValueError('Unknown bookmark targets')
        target = ','.join(ids)
    else:
        target = args.get('section') or '全部'
    target = one_line(str(target))
    if len(target) > 4000:
        raise ValueError('Oversized bookmark target')
    if ERROR.match(body):
        if body.startswith('没找到编号 '):
            body = body.replace('可能已经进了快照、删除，或已撤回。', '可能已经整理、删除或撤回。', 1)
        summary = one_line(body, 500)
    elif action in ('add', 'update'):
        match = re.fullmatch(r'已(记下|改写)【([^】]+)】书签 ([0-9a-f]{8})：(.*)', body, re.S)
        if (match is None or match[1] != ('记下' if action == 'add' else '改写')
                or match[2] not in SECTIONS.values()):
            raise ValueError('Unknown bookmark result')
        verb = '已记下' if action == 'add' else '已改写'
        summary = f'{verb}【{match[2]}】书签 {match[3]}：' + one_line(match[4], 30)
    elif action == 'list':
        # Previously stored tool results still use the old list heading.
        match = re.match(r'共 (\d+) 条(?:待整理书签|待进快照书签)(?:（返回前 100 条）)?(?:\n|$)', body)
        if match is None:
            raise ValueError('Unknown bookmark list')
        snippets = re.findall(r'^\[[0-9a-f]{8}\]【[^】]+】([^\n]+)', body, re.M)
        summary = match[0].strip().replace('待进快照书签', '待整理书签')
        if snippets:
            summary += '：' + '；'.join(one_line(value, 30) for value in snippets[:8])
    else:
        if not re.match(r'已撤回 \d+ 条书签(?:\n|$)', body):
            raise ValueError('Unknown bookmark withdrawal')
        summary = one_line(body, 500)
    return f'【调用工具】mark_moment({action}, {target})\n【结果摘要】{summary}'


def render_tool_card(call, result):
    """Return None for unknown names, pending calls or malformed client text."""
    if result is None:
        return None
    try:
        args = object_value(call['payload']['arguments'])
        name, args = tool_identity(call.get('tool_name'), args)
        body = result_text(result)
        if not body:
            return None
        return memory_card(args, body) if name == 'memory' else bookmark_card(args, body)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None
