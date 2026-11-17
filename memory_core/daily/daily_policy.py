"""Daily scheduling in the installation timezone, without storage or models."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import os
import re
from zoneinfo import ZoneInfo

from ..storage.clock import business_day_bounds, business_day_label


@dataclass(frozen=True)
class DailySettings:
    backfill_days: int = 30
    recent_diaries: int = 3
    run_time: str | None = None
    extraction_backfill_days: int = 30

    def __post_init__(self):
        if (
            type(self.extraction_backfill_days) is not int
            or not 1 <= self.extraction_backfill_days <= 30
        ):
            raise ValueError("抽卡补写天数必须为 1–30 的整数")
        if type(self.backfill_days) is not int or self.backfill_days <= 0:
            raise ValueError("日记补写天数必须是正整数")
        if type(self.recent_diaries) is not int or self.recent_diaries < 0:
            raise ValueError("日记注入篇数必须是非负整数")
        if self.run_time is not None and (
            not isinstance(self.run_time, str)
            or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.run_time)
        ):
            raise ValueError("每日任务时间须为 HH:MM，时区沿用记忆核心配置")

    @classmethod
    def from_env(cls):
        try:
            return cls(
                backfill_days=int(os.environ.get("MEMORY_DIARY_BACKFILL_DAYS", "30")),
                recent_diaries=int(os.environ.get("MEMORY_DIARY_RECENT_COUNT", "3")),
                run_time=os.environ.get("MEMORY_DAILY_RUN_TIME") or None,
                extraction_backfill_days=int(
                    os.environ.get("MEMORY_EXTRACTION_BACKFILL_DAYS", "30")
                ),
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("日记与每日任务配置无效，请核对天数、篇数和时间") from exc

    def scheduled_at(self, label, settings):
        hour, minute = (
            (int(part) for part in self.run_time.split(":"))
            if self.run_time
            else (settings.day_start_hour, 15)
        )
        if (hour, minute) <= (settings.day_start_hour, 0):
            raise ValueError("每日任务时间必须排在业务日分界之后")
        zone = ZoneInfo(settings.timezone)
        # Normalize missing spring-forward times; repeated hours run once via
        # the stable date key, using their first occurrence like the boundary.
        scheduled = datetime.combine(label, time(hour, minute), zone)
        return scheduled.astimezone(timezone.utc).astimezone(zone)

    def due_date(self, now, settings):
        label = business_day_label(now, settings)
        if now.astimezone(timezone.utc) < self.scheduled_at(label, settings).astimezone(
            timezone.utc
        ):
            label -= timedelta(days=1)
        return label

    def diary_range(self, now, settings):
        return self._range(now, settings, self.backfill_days)

    def extraction_range(self, now, settings):
        return self._range(now, settings, self.extraction_backfill_days)

    def _range(self, now, settings, days):
        end = self.due_date(now, settings) - timedelta(days=1)
        return end - timedelta(days=days - 1), end
