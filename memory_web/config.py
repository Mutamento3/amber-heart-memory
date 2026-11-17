"""Explicit web configuration; never load private project config or dotenv."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit
from memory_core.conversation_rounds import DEFAULT_TURN_TIMEOUT_SECONDS
from memory_core.model_config import ModelRole, RecallModelsConfig

@dataclass(frozen=True)
class WebSettings:
    dsn: str
    config_dsn: str
    origin: str
    password_file: Path
    local_http: bool = False
    fixed_models_file: Path | None = None
    access_key_master: str | None = field(default=None, repr=False)
    upstream_base_url: str | None = None
    upstream_api_key: str | None = field(default=None, repr=False)
    turn_timeout_seconds: int = DEFAULT_TURN_TIMEOUT_SECONDS
    recall_timeout_seconds: float = 8
    previous_day_messages: int = 4
    summary_model: str | None = None
    summary_backup_model: str | None = None
    summary_base_url: str | None = None
    summary_api_key: str | None = field(default=None, repr=False)
    diary_model: str | None = None
    diary_backup_model: str | None = None
    diary_base_url: str | None = None
    diary_api_key: str | None = field(default=None, repr=False)
    recall_models: RecallModelsConfig = field(default_factory=RecallModelsConfig)
    extraction_model: str | None = None
    extraction_base_url: str | None = None
    extraction_api_key: str | None = field(default=None, repr=False)
    topic_summary_model: str | None = None
    topic_summary_base_url: str | None = None
    topic_summary_api_key: str | None = field(default=None, repr=False)

    image_description_model: str | None = None
    image_description_backup_model: str | None = None
    image_description_base_url: str | None = None
    image_description_api_key: str | None = field(default=None,repr=False)
    image_description_timeout_seconds: float = 30
    image_description_concurrency: int = 2
    image_description_max_pending: int = 4
    image_description_max_images: int = 32
    summary_timeout_seconds: float = 120
    diary_timeout_seconds: float = 300
    extraction_timeout_seconds: float = 600
    topic_summary_timeout_seconds: float = 300
    receipt_judge_model: str | None = None
    receipt_judge_base_url: str | None = None
    receipt_judge_api_key: str | None = field(default=None, repr=False)
    receipt_judge_timeout_seconds: float = 120
    recent_notes_tidy_model: str | None = None
    recent_notes_tidy_base_url: str | None = None
    recent_notes_tidy_api_key: str | None = field(default=None, repr=False)
    recent_notes_tidy_timeout_seconds: float = 300
    recent_notes_soft_limit: int = 3500
    recent_notes_target: int = 3000
    import_concurrency: int = 3

    def __post_init__(self):
        ModelRole(
            self.extraction_model, self.extraction_base_url, self.extraction_api_key
        )
        ModelRole(self.topic_summary_model,self.topic_summary_base_url,self.topic_summary_api_key)
        ModelRole(self.receipt_judge_model,self.receipt_judge_base_url,self.receipt_judge_api_key)
        ModelRole(self.recent_notes_tidy_model,self.recent_notes_tidy_base_url,self.recent_notes_tidy_api_key)
        from memory_core.recent_note_tidy import TidyPolicy
        TidyPolicy(self.recent_notes_soft_limit, self.recent_notes_target)
        ModelRole(self.image_description_model,self.image_description_base_url,self.image_description_api_key)
        ModelRole(self.image_description_backup_model)
        if self.image_description_backup_model and not self.image_description_model:
            raise ValueError('配置备用图片描述型号时须先指定主型号')
        import math
        if (type(self.image_description_timeout_seconds) not in (int,float)
                or not math.isfinite(self.image_description_timeout_seconds) or self.image_description_timeout_seconds<=0):
            raise ValueError('图片描述调用时限必须为正数')
        if type(self.image_description_concurrency) is not int or not 1<=self.image_description_concurrency<=32:
            raise ValueError('图片描述并发数必须为 1–32 的整数')
        if type(self.image_description_max_pending) is not int or not 1<=self.image_description_max_pending<=256:
            raise ValueError('图片描述待处理消息数必须为 1–256 的整数')
        if type(self.image_description_max_images) is not int or not 1<=self.image_description_max_images<=128:
            raise ValueError('每条消息后台描述的图片上限必须为 1–128 的整数')
        origin = urlsplit(self.origin)
        if (
            origin.scheme not in ("http", "https")
            or not origin.hostname
            or origin.path
            or origin.query
            or origin.fragment
            or origin.username
            or origin.password
        ):
            raise ValueError("MEMORY_WEB_ORIGIN 必须是无路径的完整来源地址")
        if origin.scheme != "https" and not (
            self.local_http
            and origin.hostname in ("localhost", "127.0.0.1", "[::1]", "::1")
        ):
            raise ValueError("公网管理网页必须使用 HTTPS；本机验收可显式允许回环 HTTP")
        if not self.password_file.is_absolute():
            raise ValueError("密码哈希文件必须使用绝对路径")
        if not self.dsn or not self.config_dsn:
            raise ValueError("必须分别配置 MEMORY_DSN 和 MEMORY_CONFIG_DSN")
        if type(self.turn_timeout_seconds) is not int or self.turn_timeout_seconds <= 0:
            raise ValueError('MEMORY_PROXY_TURN_TIMEOUT_SECONDS 必须是正整数')
        import math
        for name in ("summary", "diary", "extraction", "topic_summary", "receipt_judge", "recent_notes_tidy"):
            value = getattr(self, name + "_timeout_seconds")
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("MEMORY_" + name.upper() + "_TIMEOUT_SECONDS 必须是有限正数")
        if type(self.recall_timeout_seconds) not in (int, float) or not math.isfinite(self.recall_timeout_seconds) or self.recall_timeout_seconds <= 0:
            raise ValueError('MEMORY_PROXY_RECALL_TIMEOUT_SECONDS 必须是正数')
        if type(self.previous_day_messages) is not int or not 0 <= self.previous_day_messages <= 1000:
            raise ValueError('MEMORY_PROXY_PREVIOUS_DAY_MESSAGES 必须为 0–1000 的整数')
        if type(self.import_concurrency) is not int or self.import_concurrency < 1:
            raise ValueError('MEMORY_IMPORT_CONCURRENCY 必须是正整数')
        if bool(self.upstream_base_url) != bool(self.upstream_api_key):
            raise ValueError('必须同时配置上游地址和上游钥匙')
        if self.upstream_base_url:
            upstream = urlsplit(self.upstream_base_url)
            if (upstream.scheme not in ('http','https') or not upstream.hostname
                or upstream.username or upstream.password or upstream.query or upstream.fragment):
                raise ValueError('上游地址必须是无凭据、查询参数和片段的 HTTP(S) 地址')
            if not self.access_key_master:
                raise ValueError('启用代理必须配置访问钥匙主密钥')
        for model in (
            self.summary_model,
            self.summary_backup_model,
            self.diary_model,
            self.diary_backup_model,
        ):
            if model is not None and (
                not isinstance(model, str) or not model.strip() or len(model) > 256
            ):
                raise ValueError("摘要或日记型号须为 1–256 字符的非空名称")
        if self.summary_backup_model and not self.summary_model:
            raise ValueError("配置备用摘要型号时须先指定主摘要型号")
        if bool(self.summary_base_url) != bool(self.summary_api_key):
            raise ValueError("摘要专用上游地址和钥匙必须同时配置；留空复用主上游")
        if self.summary_base_url:
            endpoint = urlsplit(self.summary_base_url)
            if (
                endpoint.scheme not in ("http", "https")
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError(
                    "摘要上游地址须为无凭据、查询参数和片段的 HTTP(S) 地址"
                )
        if self.diary_backup_model and not self.diary_model:
            raise ValueError("配置备用日记型号时须先指定主日记型号")
        if bool(self.diary_base_url) != bool(self.diary_api_key):
            raise ValueError("日记专用上游地址和钥匙必须同时配置；留空复用主上游")
        if self.diary_base_url:
            endpoint = urlsplit(self.diary_base_url)
            if (
                endpoint.scheme not in ("http", "https")
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError(
                    "日记上游地址须为无凭据、查询参数和片段的 HTTP(S) 地址"
                )

    @property
    def secure(self):
        return self.origin.startswith("https://")

    @property
    def cookie(self):
        return "__Host-memory-admin" if self.secure else "memory-admin-local"

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.environ.get(name)
            if not value:
                raise ValueError("未设置 " + name)
            return value

        fixed = os.environ.get("MEMORY_WEB_FIXED_MODELS_FILE")
        try:
            turn_timeout = int(os.environ.get('MEMORY_PROXY_TURN_TIMEOUT_SECONDS', str(DEFAULT_TURN_TIMEOUT_SECONDS)))
        except ValueError:
            raise ValueError('MEMORY_PROXY_TURN_TIMEOUT_SECONDS 必须是正整数') from None
        try:
            recall_timeout = float(os.environ.get('MEMORY_PROXY_RECALL_TIMEOUT_SECONDS', '8'))
            previous_messages = int(os.environ.get('MEMORY_PROXY_PREVIOUS_DAY_MESSAGES', '4'))
        except ValueError:
            raise ValueError('召回总时限或前日原话条数格式无效') from None
        model_timeouts = {}
        for name in ("summary", "diary", "extraction", "topic_summary", "receipt_judge", "recent_notes_tidy"):
            field_name = name + "_timeout_seconds"
            env_name = "MEMORY_" + field_name.upper()
            try:
                model_timeouts[field_name] = float(os.environ.get(env_name, str(getattr(cls, field_name))))
            except ValueError:
                raise ValueError(env_name + " 必须是有限正数") from None
        try:
            image_timeout=float(os.environ.get('MEMORY_IMAGE_DESCRIPTION_TIMEOUT_SECONDS','30'))
            image_concurrency=int(os.environ.get('MEMORY_IMAGE_DESCRIPTION_CONCURRENCY','2'))
            image_pending=int(os.environ.get('MEMORY_IMAGE_DESCRIPTION_MAX_PENDING','4'))
            image_max=int(os.environ.get('MEMORY_IMAGE_DESCRIPTION_MAX_IMAGES','32'))
        except ValueError:
            raise ValueError('图片描述时限、并发或待处理数量配置无效') from None
        try:
            recent_limit = int(os.environ.get('MEMORY_RECENT_NOTES_SOFT_LIMIT', '3500'))
            recent_target = int(os.environ.get('MEMORY_RECENT_NOTES_TARGET', '3000'))
        except ValueError:
            raise ValueError('秘书字数配置必须是整数') from None
        try:
            import_concurrency = int(os.environ.get('MEMORY_IMPORT_CONCURRENCY', str(cls.import_concurrency)))
        except ValueError:
            raise ValueError('MEMORY_IMPORT_CONCURRENCY 必须是正整数') from None
        return cls(
            required("MEMORY_DSN"),
            required("MEMORY_CONFIG_DSN"),
            required("MEMORY_WEB_ORIGIN"),
            Path(required("MEMORY_WEB_PASSWORD_FILE")),
            os.environ.get("MEMORY_WEB_LOCAL_HTTP") == "1",
            Path(fixed) if fixed else None,
            os.environ.get("MEMORY_ACCESS_KEY_MASTER_KEY") or None,
            os.environ.get("MEMORY_UPSTREAM_BASE_URL") or None,
            os.environ.get("MEMORY_UPSTREAM_API_KEY") or None,
            turn_timeout,
            recall_timeout,
            previous_messages,
            os.environ.get("MEMORY_SUMMARY_MODEL") or None,
            os.environ.get("MEMORY_SUMMARY_BACKUP_MODEL") or None,
            os.environ.get("MEMORY_SUMMARY_BASE_URL") or None,
            os.environ.get("MEMORY_SUMMARY_API_KEY") or None,
            os.environ.get("MEMORY_DIARY_MODEL") or None,
            os.environ.get("MEMORY_DIARY_BACKUP_MODEL") or None,
            os.environ.get("MEMORY_DIARY_BASE_URL") or None,
            os.environ.get("MEMORY_DIARY_API_KEY") or None,
            RecallModelsConfig.from_env(),
            os.environ.get("MEMORY_EXTRACTION_MODEL") or None,
            os.environ.get("MEMORY_EXTRACTION_BASE_URL") or None,
            os.environ.get("MEMORY_EXTRACTION_API_KEY") or None,
            os.environ.get("MEMORY_TOPIC_SUMMARY_MODEL") or None,
            os.environ.get("MEMORY_TOPIC_SUMMARY_BASE_URL") or None,
            os.environ.get("MEMORY_TOPIC_SUMMARY_API_KEY") or None,
            os.environ.get('MEMORY_IMAGE_DESCRIPTION_MODEL') or None,
            os.environ.get('MEMORY_IMAGE_DESCRIPTION_BACKUP_MODEL') or None,
            os.environ.get('MEMORY_IMAGE_DESCRIPTION_BASE_URL') or None,
            os.environ.get('MEMORY_IMAGE_DESCRIPTION_API_KEY') or None,
            image_timeout,image_concurrency,image_pending,image_max,
            **model_timeouts,
            receipt_judge_model=os.environ.get('MEMORY_RECEIPT_JUDGE_MODEL') or None,
            receipt_judge_base_url=os.environ.get('MEMORY_RECEIPT_JUDGE_BASE_URL') or None,
            receipt_judge_api_key=os.environ.get('MEMORY_RECEIPT_JUDGE_API_KEY') or None,
            recent_notes_tidy_model=os.environ.get('MEMORY_RECENT_NOTES_TIDY_MODEL') or None,
            recent_notes_tidy_base_url=os.environ.get('MEMORY_RECENT_NOTES_TIDY_BASE_URL') or None,
            recent_notes_tidy_api_key=os.environ.get('MEMORY_RECENT_NOTES_TIDY_API_KEY') or None,
            recent_notes_soft_limit=recent_limit,
            recent_notes_target=recent_target,
            import_concurrency=import_concurrency,
        )
