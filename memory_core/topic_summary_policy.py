"""Closed-month windows; no clients, private configuration or model calls."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import os
from zoneinfo import ZoneInfo

TOPIC_KEYS = ('relationship', 'others', 'work', 'interests', 'health')


def next_month(month):
    value = date.fromisoformat(month + '-01')
    return f'{value.year + (value.month == 12):04d}-{value.month % 12 + 1:02d}'


def previous_month(month):
    value = date.fromisoformat(month + '-01')
    return f'{value.year - (value.month == 1):04d}-{12 if value.month == 1 else value.month - 1:02d}'


@dataclass(frozen=True)
class TopicSummarySettings:
    minimum_cards: int = 5
    maximum_cards: int = 30

    def __post_init__(self):
        if (type(self.minimum_cards) is not int or type(self.maximum_cards) is not int
                or self.minimum_cards < 1 or self.maximum_cards < self.minimum_cards):
            raise ValueError('主题总结窗口张数必须为正整数，且上限不能小于门槛')

    @classmethod
    def from_env(cls):
        try:
            return cls(int(os.environ.get('MEMORY_TOPIC_SUMMARY_MIN_CARDS', '5')),
                       int(os.environ.get('MEMORY_TOPIC_SUMMARY_MAX_CARDS', '30')))
        except ValueError:
            raise ValueError('主题总结窗口张数配置无效') from None


def event_date(card, core):
    raw = card['source_date'] or card['created_at']
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw)
        value = datetime.fromisoformat(raw)
        zone = ZoneInfo(core.timezone)
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(zone).date()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('topic_summary_invalid_source_date:' + str(card['id'])) from None


def next_run_after(now, core, daily):
    local = now.astimezone(ZoneInfo(core.timezone))
    month = local.strftime('%Y-%m')
    run = daily.scheduled_at(date.fromisoformat(month + '-03'), core)
    if run <= now:
        run = daily.scheduled_at(date.fromisoformat(next_month(month) + '-03'), core)
    return run


def plan_windows(cards, *, cycle_month, core, policy, blocked=(), deferred=()):
    """Accumulate complete months; rejected windows must extend into a later month."""
    buckets = defaultdict(list)
    for card in cards:
        day = event_date(card, core)
        month = day.strftime('%Y-%m')
        if month < cycle_month and not any(a <= month <= b for a, b in blocked):
            buckets[month].append((day, card))
    last_closed = previous_month(cycle_month)
    moments = set(buckets)
    for a, b in deferred:
        later = next_month(b)
        if later <= last_closed:
            moments.add(later)
    windows, accumulated, start = [], [], None
    for month in sorted(moments):
        barriers = [(a, b) for a, b in blocked if start is not None and start <= b and a <= month]
        if barriers:
            if accumulated:
                windows.append(dict(start_month=start, end_month=previous_month(min(a for a, _ in barriers)),
                                    state='waiting', cards=accumulated))
            accumulated, start = [], next_month(max(b for _, b in barriers))
        rows = sorted(buckets[month], key=lambda pair: (pair[0], pair[1]['id']))
        if start is None and rows:
            start = month
        accumulated.extend(card for _, card in rows)
        if not accumulated:
            continue
        earliest_end = max([month] + [next_month(b) for a, b in deferred if start <= b and a <= month])
        if len(accumulated) >= policy.minimum_cards and month >= earliest_end:
            windows.append(dict(start_month=start, end_month=month,
                state='overflow' if len(accumulated) > policy.maximum_cards else 'ready', cards=accumulated))
            accumulated, start = [], next_month(month)
    if accumulated:
        before_reservations = [previous_month(a) for a, _ in blocked if a > start]
        windows.append(dict(start_month=start, end_month=min([last_closed]+before_reservations), state='waiting', cards=accumulated))
    return windows
