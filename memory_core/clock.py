"""Offset-bearing TEXT timestamps and configurable business-day boundaries."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区偏移")
    return value


def format_timestamp(value: datetime, settings: Settings) -> str:
    return aware(value).astimezone(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")


def business_day_bounds(label: date, settings: Settings) -> tuple[datetime, datetime]:
    zone = ZoneInfo(settings.timezone)

    def boundary(day):
        local = datetime.combine(day, time(settings.day_start_hour), zone)
        # Nonexistent spring-forward times advance to the first corresponding
        # real time. Ambiguous fall-back boundaries use the first occurrence.
        return local.astimezone(timezone.utc).astimezone(zone)

    return boundary(label), boundary(label + timedelta(days=1))


def business_day_label(value: datetime, settings: Settings) -> date:
    instant = aware(value).astimezone(timezone.utc)
    local_date = value.astimezone(ZoneInfo(settings.timezone)).date()
    start, _ = business_day_bounds(local_date, settings)
    return local_date if instant >= start.astimezone(timezone.utc) else local_date - timedelta(days=1)
