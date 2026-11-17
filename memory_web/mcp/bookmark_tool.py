"""Approved bookmark tool wording and schema; names come from the space policy."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from memory_core.bookmarks.recent_note_text import SECTIONS

DESCRIPTION = """你可以为值得记住的片刻记书签。记忆书签分【月经周期】【健康关注】【长期事项】【日常杂记】四个分区。新记的书签每天夜里会原样加进已整理的书签；已整理的书签每轮都会提供给你，帮助你了解{user_name}的近况。整理前，你可以随时查看（list）、改写（update）、撤回（delete）。已经整理的书签这里改不了，有新进展或更正就新记一条。
什么值得记：听到新信息时问自己，如果下次完全不知道这件事，会不会让你显得不懂{user_name}、忘掉重要进展或约定，或让回答明显变差？会的话就主动 add，不必等{user_name}说"记住"。月经周期、健康变化、长期事项和有延续价值的日常经历通常值得记；普通撒娇、玩笑、短暂情绪和寒暄不记。
同一件事只留一条：准备记录时，如果不确定是否已经记过同一件事，先 list 查一下。已经有了、现在有补充、进展或更正，就 update 改写原来那条：把新旧信息合成一条完整的客观事实，被更正的旧说法直接去掉，不要另起一条。发现同一件事有好几条，update 其中一条写成完整版，再 delete 其余的。记错或已不需要的直接 delete。
不要每轮例行 list，只在准备记录或修改时查。"""


class BookmarkArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['add','list','update','delete'] = Field(default='add', description='动作：add 新增、list 查看、update 改写、delete 撤回；不填按 add 处理')
    section: Literal['period','health','longterm','daily'] | None = Field(default=None,
        description='分区：period=月经周期，health=健康关注，longterm=长期事项，daily=日常杂记。add 必填；list 不填查全部；update 可选，填了就挪到新分区')
    content: StrictStr | None = Field(default=None,
        description='要保存的客观事实，开头写事情发生的日期（如2026年11月9日）；记下以后用得上的结论、约定和进展就好，不用把过程和现场细节都写进去，写法可以参照已整理的书签。不写感想或对话复述。update 时写改写后的完整内容。')
    ids: list[StrictStr] | None = Field(default=None, description='书签编号（list 返回的短编号）。update 填 1 个；delete 可填多个。')


def description(policy):
    return DESCRIPTION.format(user_name=policy['participants']['user']['name'])


def render_bookmarks(action, result):
    if action=='list':
        lines = [f"共 {result['total']} 条待整理书签" + ('（返回前 100 条）' if result['has_more'] else '')]
        lines += [f"[{item['id']}]【{item['section_label']}】{item['content']}\n记录时间：{item['created_at']}\n完整编号：{item['full_id']}" for item in result['items']]
        return '\n'.join(lines)
    if action in ('add','update') and result['ok']:
        return f"已{'记下' if action=='add' else '改写'}【{SECTIONS[result['section']]}】书签 {result['id']}：{result['content']}"
    if action=='update':
        return '\n'.join(result.get('errors', [])) or '改写书签失败，请重新查看待整理书签'
    return '\n'.join([f"已撤回 {len(result.get('deleted', []))} 条书签", *result.get('errors', [])])
