"""Public MCP tool definitions and Chinese results used by history backfill."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from memory_core.conversations import chat_text


MEMORY_DESCRIPTION = """自主查阅{user_name}已经审核入库的个人记忆，用真实的共同经历、偏好和约定回应{user_name}，而不是让{user_name}反复提醒你。
搜索能找到归档记忆；单独 get 打开因长期未使用而归档的记忆，会自动恢复并重新计时。

回答前先问自己：当前对话和已注入回忆是否已经足够？如果缺少某段旧事会让你误解{user_name}、忘掉共同经历或承诺、重复问{user_name}答过的问题，或让这次回答明显变差，就主动查询，不必等{user_name}说“查记忆”。需要回顾一段时间、核对旧偏好或普通召回缺了关键细节时也应使用。当前信息已经够用时就直接回答；不要因为亲昵、反问或修辞里偶然出现“今年”“变化”等词机械查档。

怎么选：
- 记得具体字眼，比如人名、书名、游戏名、地名、典故，或{user_name}当时的原话 → 用 browse 加 text 字面查找。在当前筛选范围内按原文字面匹配，不受相关度排名影响；优先选最独特的一个原词，不要用空格拼接多个关键词。
- 只记得大概意思、说不出原词 → 用 search。search 按和整句查询的相关度排名，默认先返回排名靠前的一页，所以一次只查一件事。如果把一件具体旧事和一个大话题写在同一句里，大话题下的大量记忆可能会把那件具体旧事挤出名单。要查两件事，就分两次查，可以在同一轮并行调用。
- 想按主题、实体或日期整体回顾 → browse。
- 想展开某篇主题总结的细节 → get_sources；有 next_cursor 时继续翻页。想看某一张卡片的全文 → get。

search 没找到不代表没有：可以换成 browse 字面查，或者把查询缩小到一件事再试。

不确定主题名时先 list_topics。browse/search 默认只看原子记忆，并排除已过时的；问历史变化时才纳入过时记忆，并把它当作旧版本。命中的卡片如果已被某篇主题总结收录，会标出那篇总结的编号；想看总结时用 source_scope=topic_summary（只看总结）或 all（两者都看），也可以按那个编号 get。这里的完整正文是审核后的记忆卡片，不是原始聊天逐字稿。

查到的记忆同样只供参考：它们是过去某个时间节点的切片，不等于{user_name}当下的想法，不能当作现成素材直接回答这一轮。除了客观事实与明确的待办约定，要根据当前上下文和你这一轮真正的思考与观点来回应。"""


class MemoryArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action: Literal['search', 'browse', 'get', 'get_sources', 'list_topics'] = Field(
        description='search=相关搜索；browse=完整浏览；get=读单张全文；get_sources=回查主题总结来源；list_topics=列出有内容的主题')
    query: Annotated[StrictStr, Field(max_length=300)] | None = Field(default=None, description='search 时必填，一次只写一件要查的事')
    topic: Annotated[StrictStr, Field(max_length=80)] | None = Field(default=None, description='browse/search 可选，使用 list_topics 中的主题名称')
    entity: Annotated[StrictStr, Field(max_length=80)] | None = Field(default=None, description='browse/search 可选，实体名或登记的别名；不确定时省略')
    text: Annotated[StrictStr, Field(max_length=100)] | None = Field(default=None, description='browse 可选，字面匹配标题、摘要和正文中的连续文字；不要空格拼接多个词')
    date_from: Annotated[StrictStr, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')] | None = Field(default=None, description='browse/search 可选，起始日期 YYYY-MM-DD，含当天')
    date_to: Annotated[StrictStr, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')] | None = Field(default=None, description='browse/search 可选，结束日期 YYYY-MM-DD，含当天')
    include_outdated: StrictBool = Field(default=False, description='browse/search 可选，仅查询历史版本或变化过程时设为 true')
    source_scope: Literal['atomic', 'topic_summary', 'all'] | None = Field(default=None, description='list_topics 必填；browse/search 默认 atomic，topic_summary=只看总结，all=两者')
    order: Literal['asc', 'desc'] = Field(default='asc', description='browse 的日期顺序，默认从旧到新')
    limit: Annotated[StrictInt, Field(ge=1, le=50)] | None = Field(default=None, description='每页数量：browse 默认30/最多50，search 默认10/最多20，get_sources 默认20/最多30')
    cursor: Annotated[StrictInt, Field(ge=0, le=2147483647)] = Field(default=0, description='翻页位置，使用上次结果的 next_cursor')
    memory_id: Annotated[StrictStr, Field(pattern=r'^M-[1-9][0-9]*$', max_length=12)] | None = Field(default=None, description='get/get_sources 必填，来自结果或召回的 M-编号，例如 M-123')


def memory_description(policy):
    return MEMORY_DESCRIPTION.format(user_name=policy['participants']['user']['name'],
                                     assistant_name=policy['participants']['assistant']['name'])


def public_card(row, *, full):
    is_summary = row['item_kind'] == 'topic_summary'
    return dict(memory_id=f"M-{row['id']}", title=row.get('title') or chat_text.UNTITLED,
                date=row.get('source_date') or chat_text.UNDATED, topics=row.get('topics', []),
                item_kind=row['item_kind'], cognitive_status=row.get('cognitive_status', 'stable'),
                archive_reason=row.get('archive_reason'), parent_memory_id=(
                    f"M-{row['parent_reflection_id']}" if row.get('parent_reflection_id') else None),
                experience_mode=row.get('experience_mode'), is_milestone=bool(row.get('is_milestone')),
                content=row['content'] if full or is_summary else (row.get('summary') or row['content'])[:360])


def render_result(action, result):
    if action == 'list_topics':
        topics = result['topics']
        return '当前记忆池中有内容的主题：' + ('、'.join(topics) if topics else '暂无'), dict(action=action, topics=topics)
    rows = [result['item']] if action == 'get' else result['items']
    cards = [public_card(row, full=action in ('get', 'get_sources')) for row in rows]
    lines = [dict(get='读取记忆全文', browse='浏览记忆', search='搜索相关记忆', get_sources='回查主题总结来源')[action]]
    if action == 'get_sources':
        lines.append(f"主题总结：M-{result['parent']['id']} {result['parent'].get('title') or chat_text.UNTITLED}")
    if not cards:
        lines.append('没有找到符合条件的记忆。')
    for card in cards:
        lines += ['', chat_text.CARD_HEADER.format(number=card['memory_id'], title=card['title'], date=card['date']),
                  '主题：' + ('、'.join(card['topics']) or '未注明')]
        if card['item_kind'] == 'topic_summary':
            lines.append('类型：主题总结，可用 get_sources 回查来源卡片。')
        if card['parent_memory_id']:
            lines.append(f"所属主题总结：{card['parent_memory_id']}")
        if card['archive_reason']:
            lines.append('状态：已归档，get 打开全文会自动恢复。')
        if card['cognitive_status'] == 'outdated':
            lines.append('历史版本：不能当作当前事实。')
        if card['cognitive_status'] == 'disputed':
            lines.append('存在争议：回答时保留不确定性。')
        if card['is_milestone']:
            lines.append('里程碑：是')
        if card['experience_mode'] in chat_text.EXPERIENCE:
            lines.append(chat_text.EXPERIENCE[card['experience_mode']])
        lines.append(card['content'])
    if action != 'get':
        lines.append(f"\n本页 {len(cards)} 条，共 {result['total_count']} 条。")
    if result.get('next_cursor') is not None:
        lines.append(f"还有下一页，请使用 cursor={result['next_cursor']}。")
    if action == 'search':
        ordering = result['ordering_stats']
        if ordering.get('error'):
            lines.append('第一级文档精排失败，已退回三路粗检顺序。')
        if result['retrieval_stats'].get('errors'):
            lines.append('部分检索步骤暂不可用，当前结果来自已完成的检索。')
        lines.append('这是相关候选，不是完整清单；完整盘点请用 browse。')
    lines.append('这些是审核后的记忆卡片，不是原始聊天逐字稿。')
    data = dict(action=action, items=cards)
    for key in ('total_count', 'cursor', 'next_cursor'):
        if key in result:
            data[key] = result[key]
    return '\n'.join(lines), data
