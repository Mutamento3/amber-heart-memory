"""Explicit server-side model roles; no dotenv, private defaults or model choice."""

from dataclasses import dataclass, field
import math
import os
from urllib.parse import urlsplit


DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class ModelRole:
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.model is not None and (
            not isinstance(self.model, str)
            or not self.model.strip()
            or len(self.model) > 256
        ):
            raise ValueError("模型名称须为 1–256 字符的非空文本")
        if bool(self.base_url) != bool(self.api_key):
            raise ValueError("岗位专用地址和 key 必须同时填写；同时留空才复用主上游")
        if self.base_url:
            value = urlsplit(self.base_url)
            if (
                value.scheme not in ("http", "https")
                or not value.hostname
                or value.username
                or value.password
                or value.query
                or value.fragment
            ):
                raise ValueError(
                    "模型地址必须是不含凭据、查询参数和片段的 HTTP(S) 基址"
                )

    def resolve(self, main_url=None, main_key=None):
        return ModelRole(
            self.model, self.base_url or main_url, self.api_key or main_key
        )

    @property
    def configured(self):
        return bool(self.model and self.base_url and self.api_key)


@dataclass(frozen=True)
class RecallModelsConfig:
    embedding: ModelRole = field(default_factory=ModelRole)
    cleaner: ModelRole = field(default_factory=ModelRole)
    reranker: ModelRole = field(default_factory=ModelRole)
    selector: ModelRole = field(default_factory=ModelRole)
    timeout_seconds: float = 5
    document_rerank_format: str = "voyage"

    def __post_init__(self):
        if self.document_rerank_format not in ("voyage", "cohere"):
            raise ValueError("MEMORY_DOCUMENT_RERANK_FORMAT 必须是 voyage 或 cohere")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("召回岗位单次超时必须是正数")

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        roles = {}
        for name, prefix in (
            ("embedding", "EMBEDDING"),
            ("cleaner", "KEYWORD"),
            ("reranker", "DOCUMENT_RERANK"),
            ("selector", "FINAL_RERANK"),
        ):
            roles[name] = ModelRole(
                *(
                    env.get("MEMORY_" + prefix + suffix) or None
                    for suffix in ("_MODEL", "_BASE_URL", "_API_KEY")
                )
            )
        try:
            timeout_seconds = float(
                env.get("MEMORY_RECALL_MODEL_TIMEOUT_SECONDS", "5")
            )
        except ValueError:
            raise ValueError("召回岗位超时配置无效") from None
        return cls(
            **roles,
            timeout_seconds=timeout_seconds,
            document_rerank_format=env.get("MEMORY_DOCUMENT_RERANK_FORMAT", "voyage"),
        )
