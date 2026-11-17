"""Existing content layout and selection priorities without private examples."""

def format_document(mem: dict, limit_chars: int = 600) -> str:
    """把记忆整理成 reranker 友好的单段文本。"""
    parts = []
    title = (mem.get("title") or "").strip()
    date = (mem.get("date") or mem.get("source_date") or "").strip()
    experience_mode = (mem.get("experience_mode") or "").strip()
    content = (mem.get("content") or "").strip()

    if title:
        parts.append(f"标题: {title}")
    if date:
        parts.append(f"日期: {date}")
    if experience_mode == "shared":
        parts.append("经历层: 共同赛博经历（真实关系互动，但不代表物理发生）")
    elif experience_mode == "dream":
        parts.append("经历层: 睡梦（梦境内容不可当作外部现实）")
    elif experience_mode == "imagined":
        parts.append("经历层: 想象或创作内容（不可当作已发生事实）")
    if content:
        parts.append(f"内容: {content[:limit_chars]}")
    if mem.get("item_kind") == "topic_summary" and mem.get("_rerank_evidence"):
        parts.append(f"【本轮命中的来源记忆】\n{mem['_rerank_evidence']}")
    return "\n".join(parts) or content[:limit_chars]

def ranking_query(query: str) -> str:
    """把用户原话包成 Voyage rerank 指令。"""
    return f"""你是 记忆检索重排器。请从候选记忆中选出最适合注入当前对话的记忆。

判断优先级：
1. 相关性优先：必须与当前消息的真实意图直接相关，不要被称呼、语气词或泛泛措辞干扰。
2. 时间参考：近期记忆可略优先；但更早的关键事件、长期规则、稳定偏好仍然可以排在前面。
3. 经历层只帮助理解事实边界，不是固定降权：external 与 shared 都是正常记忆；shared 是真实共同参与的赛博关系经历，但不表示物理发生。dream 与 imagined 在当前问题确实相关时仍可优先，但不得当作外部现实事实。
4. 只按单条候选与当前消息的匹配度打分；不要为了凑满数量而提高弱相关记忆。

当前消息：{query}"""


def selection_prompt(query, memories, top_k=5):
    lines = []
    for i, mem in enumerate(memories, 1):
        meta = []
        if mem.get('source_date'):
            meta.append(f"[日期:{mem['source_date']}]")
        if mem.get('importance') is not None:
            meta.append(f"[重要性:{mem['importance']}]")
        mode = mem.get('experience_mode')
        if mode == 'shared':
            meta.append('[经历层:共同赛博经历，非物理事件]')
        elif mode in {'dream','imagined'}:
            label = '睡梦' if mode=='dream' else '想象/创作'
            meta.append(f'[经历层:{label}，非外部现实]')
        title = (mem.get('title') or '').strip()
        body = mem.get('content') or ''
        if mem['item_kind'] != 'topic_summary':
            body = body[:200]
        elif mem.get('_rerank_evidence'):
            body += '\n【本轮命中的来源记忆】\n'+mem['_rerank_evidence']
        line = f'{i}.'
        if meta:
            line += ' '+' '.join(meta)
        if title:
            line += f' {title} —' if body else f' {title}'
        if body:
            line += ' '+body
        lines.append(line+'\n')
    candidates_text = ''.join(lines)
    return f'''你是一个记忆相关性判断助手。从下列候选记忆中, 选出最适合回答用户当前消息的 {top_k} 条。

用户当前消息: {query}

候选记忆:
{candidates_text}

【挑选规则】(按优先级)
一、相关性优先 — 选与用户当前消息直接相关的记忆。注意识别用户的真实意图, 不要被高频称呼或语气词干扰。
二、多样性 — 内容高度相似的记忆只留一条; 但同一事件的不同侧面(比如发现/原谅/和解 这种上下集)可以保留两条, 帮助还原事件全貌。
三、时间平衡 — 优先选近期发生的, 但适当兼顾更早的关键事件。
四、经历层 — external 与 shared 都正常参与，不因 shared 是赛博互动而降权；dream 与 imagined 在当前问题相关时也可选择，但不要误当作外部现实。
五、重要性参考 — 当上面四条相当时, 优先选 [重要性] 数值更高的。

【输出要求】
- 只返回 {top_k} 个序号, 用逗号分隔, 按相关性从高到低排序
- 例如: 3,7,1,5,2
- 不要解释、不要其他文字, 只返回序号

序号:'''
