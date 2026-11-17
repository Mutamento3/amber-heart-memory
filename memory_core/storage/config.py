"""Installation settings; no implicit .env loading or model configuration."""

from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# This baseline always creates a regular-vector HNSW index.
MAX_VECTOR_DIMENSIONS = 2000


@dataclass(frozen=True)
class Settings:
    dimensions: int = 1536
    timezone: str = "Asia/Shanghai"
    day_start_hour: int = 3

    def __post_init__(self):
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= MAX_VECTOR_DIMENSIONS:
            raise ValueError(
                "向量维度必须为 1–2000 的整数：本安装基线使用普通 vector 的 HNSW 索引，"
                "最多支持 2000 维。不会自动截断向量、切换索引或修改已有数据库。"
            )
        if type(self.day_start_hour) is not int or not 0 <= self.day_start_hour <= 23:
            raise ValueError("业务日分界小时必须为 0–23 的整数")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError("时区必须是有效的 IANA 时区名称") from exc

    def as_dict(self):
        return asdict(self)
