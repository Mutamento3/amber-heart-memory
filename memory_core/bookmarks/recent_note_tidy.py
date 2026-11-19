"""Established secretary prompt and result validation."""

from dataclasses import dataclass
import json
from pathlib import Path

from .recent_note_text import SECTIONS, PREFIXES, char_count, one_line


@dataclass(frozen=True)
class TidyPolicy:
    soft_limit: int = 3500
    target: int = 3000

    def __post_init__(self):
        if type(self.soft_limit) is not int or type(self.target) is not int or not 0 < self.target <= self.soft_limit:
            raise ValueError('秘书字数目标必须为正整数，且不能超过字数线')


def build_entries(sections):
    entries = {}
    for section, prefix in PREFIXES.items():
        lines = [line for line in sections.get(section, '').split('\n') if line.strip()]
        for index, text in enumerate(lines, 1):
            entries[f'{prefix}{index}'] = dict(section=section, text=text, index=index-1)
    return entries


def prepare_request(sections, policy, participants, today):
    entries = build_entries(sections)
    # Only the space's visible sections are given to the secretary.
    total = sum(char_count(sections[section]) for section in SECTIONS if section in sections)
    mode = (f'现在超过字数线，要提删减建议，目标是让记忆书签降到{policy.target}字左右，低一点也可以。'
            if total > policy.soft_limit else
            '现在没有超过字数线：只做合并和精简（规则一、二、三），不要提删除建议，规则四不适用。')
    prompt = (Path(__file__).parent.parent / 'prompts' / 'snapshot_tidy.md').read_text(encoding='utf-8')
    values = dict(USER_NAME=participants['user']['name'], ASSISTANT_NAME=participants['assistant']['name'],
                  TODAY=today.strftime('%Y年%m月%d日'), TOTAL_CHARS=total, SOFT_LIMIT=policy.soft_limit, MODE_INSTRUCTION=mode)
    for key, value in values.items():
        prompt = prompt.replace('{{' + key + '}}', str(value))
    material = '\n\n'.join(f'【{label}】（{char_count(sections.get(section, ""))}字）\n' + '\n'.join(
        f'【{key}】{entry["text"]}' for key, entry in entries.items() if entry['section']==section)
        for section, label in SECTIONS.items() if section in sections)
    return prompt, material, entries


def parse_result(content):
    try:
        value, _ = json.JSONDecoder().raw_decode(content[content.index('{'):])
        if (not isinstance(value, dict) or not isinstance(value.get('suggestions'), list)
                or not isinstance(value.get('need_confirm', []), list)):
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise ValueError('秘书返回的 JSON 格式有误，请重新整理') from None


def validate_result(data, entries, *, allow_delete):
    suggestions, need_confirm, dropped, used = [], [], [], set()
    for index, item in enumerate(data['suggestions'], 1):
        reason = ''
        if not isinstance(item, dict):
            reason = '建议格式无效'
        else:
            targets = item.get('targets')
            kind, text = item.get('type'), item.get('new_text')
            if not isinstance(kind, str) or kind not in {'合并','精简','压缩','删除'}:
                reason = '改动类型无效'
            elif not isinstance(targets, list) or not targets or any(not isinstance(t, str) or t not in entries for t in targets):
                reason = '编号不存在或目标为空'
            elif len(set(targets))!=len(targets) or set(targets) & used:
                reason = '与前面的建议重叠或重复引用同一条原文'
            elif len({entries[t]['section'] for t in targets})!=1:
                reason = '不能跨分区合并'
            elif not isinstance(text, str) or (kind=='删除' and text.strip()) or (kind!='删除' and not text.strip()):
                reason = '删除建议不能带新正文，其他建议的新正文不能为空'
            elif kind=='删除' and not allow_delete:
                reason = '未超过字数线，本次不能提出删除建议'
            elif kind=='删除' and any(entries[t]['text'].lstrip().startswith('★') for t in targets):
                reason = '带星号的条目不能删除'
            elif any(f'【{marker}】' in text for marker in SECTIONS.values()):
                reason = '新正文不能包含分区标题'
        if reason:
            dropped.append(dict(kind='suggestion', item=item, reason=reason))
            continue
        new_text = one_line(text) if kind!='删除' else ''
        if new_text and any(entries[t]['text'].lstrip().startswith('★') for t in targets) and not new_text.startswith('★'):
            new_text = '★' + new_text
        used.update(targets)
        suggestions.append(dict(id=f'change-{index}', no=index, type=kind, targets=targets,
            section=entries[targets[0]]['section'], new_text=new_text, reason=str(item.get('reason') or '')))
    for item in data.get('need_confirm', []):
        if (not isinstance(item, dict) or not isinstance(item.get('target'), str) or item['target'] not in entries
                or not isinstance(item.get('question'), str) or not item['question'].strip()):
            dropped.append(dict(kind='need_confirm', item=item, reason='待确认编号不存在或问题为空'))
        else:
            need_confirm.append(dict(target=item['target'], section=entries[item['target']]['section'], question=item['question'].strip()))
    return dict(suggestions=suggestions, need_confirm=need_confirm, note=str(data.get('note') or ''), dropped=dropped)
