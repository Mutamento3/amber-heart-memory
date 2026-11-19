"""Selection and workload preview only; the durable import runner owns execution."""

from datetime import date

from .import_formats import ImportFormatError


def _date(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
        return value
    except ValueError:
        raise ImportFormatError("日期范围必须使用有效的 YYYY-MM-DD") from None


def _assistant_groups(records, assistants, names, field):
    groups = {}
    for record in records:
        key, name = assistants[record["key"]]
        group = groups.setdefault(key, dict(name=names[key], sample_titles=[], **{field: []}))
        group[field].append(record)
        if key is not None and name is None and field == "conversations":
            group["sample_titles"].append(record["title"])
    for group in groups.values():
        if group["sample_titles"]:
            group["sample_titles"] = list(dict.fromkeys(
                group["sample_titles"]))[:3]
    return list(groups.values())


def _assistant_names(records, assistants):
    names = {None: "未归属"}
    missing = 0
    for record in records:
        key, name = assistants[record["key"]]
        if key in names:
            continue
        if name is None:
            missing += 1
            name = f"助手 {missing}"
        names[key] = name
    return names


def _selected_keys(values, allowed, message):
    if (not isinstance(values, list) or any(not isinstance(key, str) for key in values)
            or not set(values) <= allowed):
        raise ImportFormatError(message)
    return set(values)


def preview(items, *, daily_limit, used_per_day=None, imported_keys=(),
            selected_conversations=None, selected_memories=None, date_start=None, date_end=None):
    """Counts exclude imported material. Chat call counts are bounds, not a quote."""
    if not items or len({item.source for item in items}) != 1:
        raise ImportFormatError("一次只能预览一种导入来源")
    if type(daily_limit) is not int or daily_limit <= 0:
        raise ImportFormatError("每日抽卡额度必须是正整数")
    source = items[0].source
    by_conversation = source in ("chatgpt", "claude", "deepseek", "kelivo")
    conversations_items = [item for item in items if not item.is_kelivo_memory]
    memory_items = [item for item in items if item.is_kelivo_memory]
    used_per_day = used_per_day or {}
    if any(type(value) is not int or value < 0 for value in used_per_day.values()):
        raise ImportFormatError("已有每日候选数量无效")
    date_start, date_end = _date(date_start), _date(date_end)
    if date_start and date_end and date_start > date_end:
        raise ImportFormatError("开始日期不能晚于结束日期")
    if source not in ("gemini", "kelivo") and (date_start or date_end):
        raise ImportFormatError("只有 Gemini 和 Kelivo 按日期范围选择")
    if not by_conversation and selected_conversations is not None:
        raise ImportFormatError("该来源不按对话选择")
    if source != "kelivo" and selected_memories is not None:
        raise ImportFormatError("该来源不单独选择记忆条目")
    keys = {item.conversation_key for item in conversations_items}
    if selected_conversations is None:
        # Require an explicit choice when a backup contains several groups.
        multiple = source == "kelivo" and len({item.assistant_key for item in conversations_items}) > 1
        selected = set() if multiple else keys
    else:
        selected = _selected_keys(selected_conversations, keys, "选择了不存在的来源对话")
    selected_entries = set() if selected_memories is None else _selected_keys(
        selected_memories, {item.source_key for item in memory_items}, "选择了不存在的记忆条目")
    imported = set(imported_keys)
    chosen, duplicates, conversations = [], 0, {}
    for item in conversations_items:
        record = conversations.setdefault(item.conversation_key, dict(
            key=item.conversation_key, title=item.title, first_date=item.business_date,
            last_date=item.business_date, day_count=0, message_count=0,
            imported_day_count=0, selected=item.conversation_key in selected))
        # Memory-file entries may be undated; chat days always have a date.
        dates = [value for value in (record["first_date"], item.business_date) if value is not None]
        record["first_date"] = min(dates, default=None)
        dates = [value for value in (record["last_date"], item.business_date) if value is not None]
        record["last_date"] = max(dates, default=None)
        record["day_count"] += 1
        record["message_count"] += item.message_count
        record["imported_day_count"] += item.source_key in imported
        in_range = (not date_start or item.business_date >= date_start) and (
            not date_end or item.business_date <= date_end)
        if item.conversation_key not in selected or not in_range:
            continue
        if item.source_key in imported:
            duplicates += 1
        else:
            chosen.append(item)
    ordered = sorted(conversations.values(), key=lambda item: (item["first_date"], item["key"])) if by_conversation else []
    memories = []
    for item in memory_items:
        selected_entry = item.source_key in selected_entries
        duplicate = item.source_key in imported
        record = dict(key=item.source_key, title=item.title, source_date=item.business_date,
                      selected=selected_entry, imported=duplicate)
        if item.memory_text is not None:
            record["text"] = item.memory_text
        memories.append(record)
        if selected_entry:
            if duplicate:
                duplicates += 1
            else:
                chosen.append(item)
    chats = [item for item in chosen if not item.is_kelivo_memory]
    days = {item.business_date for item in chats}
    minimum = len(chosen)
    dates = [item.business_date for item in conversations_items if item.business_date is not None]
    # Every conversation/day needs selection. Structure consumes the same
    # shared daily quota as live chat, not a separate quota per conversation.
    maximum = minimum if source == "memory" else minimum + sum(
        max(0, daily_limit - used_per_day.get(day, 0)) for day in days)
    result = dict(
        source=source,
        conversations=ordered if source != "kelivo" else [],
        first_date=min(dates, default=None),
        last_date=max(dates, default=None),
        date_start=date_start, date_end=date_end,
        source_keys=[item.source_key for item in chosen],
        day_count=len(days), material_count=len(chosen),
        message_count=sum(item.message_count for item in chats) if source != "memory" else 0,
        memory_count=len(chosen) if source == "memory" else len(chosen) - len(chats),
        skipped_count=duplicates,
        model_calls_min=minimum, model_calls_max=maximum,
        model_calls_with_retry_max=maximum * 2,
    )
    if source == "kelivo":
        assistants = {item.source_key if item.is_kelivo_memory else item.conversation_key:
                      (item.assistant_key, item.assistant_name) for item in items}
        names = _assistant_names(ordered + memories, assistants)
        result["assistant_groups"] = _assistant_groups(ordered, assistants, names, "conversations")
        result["memory_groups"] = _assistant_groups(memories, assistants, names, "memories")
    return result
