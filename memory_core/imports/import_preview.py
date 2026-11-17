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


def preview(items, *, daily_limit, used_per_day=None, imported_keys=(),
            selected_conversations=None, date_start=None, date_end=None):
    """Counts exclude imported material. Chat call counts are bounds, not a quote."""
    if not items or len({item.source for item in items}) != 1:
        raise ImportFormatError("一次只能预览一种导入来源")
    if type(daily_limit) is not int or daily_limit <= 0:
        raise ImportFormatError("每日抽卡额度必须是正整数")
    source = items[0].source
    used_per_day = used_per_day or {}
    if any(type(value) is not int or value < 0 for value in used_per_day.values()):
        raise ImportFormatError("已有每日候选数量无效")
    date_start, date_end = _date(date_start), _date(date_end)
    if date_start and date_end and date_start > date_end:
        raise ImportFormatError("开始日期不能晚于结束日期")
    if source != "gemini" and (date_start or date_end):
        raise ImportFormatError("只有 Gemini 按日期范围选择")
    if source in ("memory", "gemini") and selected_conversations is not None:
        raise ImportFormatError("该来源不按对话选择")
    keys = {item.conversation_key for item in items}
    if selected_conversations is None:
        selected = keys
    elif (not isinstance(selected_conversations, list)
            or any(not isinstance(key, str) for key in selected_conversations)
            or not set(selected_conversations) <= keys):
        raise ImportFormatError("选择了不存在的来源对话")
    else:
        selected = set(selected_conversations)
    imported = set(imported_keys)
    chosen, duplicates, conversations = [], 0, {}
    for item in items:
        record = conversations.setdefault(item.conversation_key, dict(
            key=item.conversation_key, title=item.title, first_date=item.business_date,
            last_date=item.business_date, day_count=0, message_count=0,
            imported_day_count=0, selected=item.conversation_key in selected))
        record["first_date"] = min(record["first_date"], item.business_date)
        record["last_date"] = max(record["last_date"], item.business_date)
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
    days = {item.business_date for item in chosen}
    minimum = len(chosen)
    # Every conversation/day needs selection. Structure consumes the same
    # shared daily quota as live chat, not a separate quota per conversation.
    maximum = minimum if source == "memory" else minimum + sum(
        max(0, daily_limit - used_per_day.get(day, 0)) for day in days)
    return dict(
        source=source,
        conversations=sorted(conversations.values(), key=lambda item: (item["first_date"], item["key"]))
            if source in ("chatgpt", "claude") else [],
        first_date=min(item.business_date for item in items),
        last_date=max(item.business_date for item in items),
        date_start=date_start, date_end=date_end,
        source_keys=[item.source_key for item in chosen],
        day_count=len(days), material_count=len(chosen),
        message_count=sum(item.message_count for item in chosen) if source != "memory" else 0,
        memory_count=len(chosen) if source == "memory" else 0,
        skipped_count=duplicates,
        model_calls_min=minimum, model_calls_max=maximum,
        model_calls_with_retry_max=maximum * 2,
    )
