"""Optional nightly receipt judge; no model is selected automatically."""

from memory_core.models.completion_model import CompletionModel, CompletionModelError, no_thinking_options
from memory_core.models.model_config import ModelRole
from .extraction_model import safe_usage


class ReceiptJudgeModel:
    def __init__(self, settings, *, transport=None):
        self.role = ModelRole(settings.receipt_judge_model, settings.receipt_judge_base_url,
            settings.receipt_judge_api_key).resolve(settings.upstream_base_url, settings.upstream_api_key)
        self.transport = transport
        self.timeout = settings.receipt_judge_timeout_seconds

    @property
    def configured(self):
        return bool(self.role.model and self.role.configured)

    def judge(self, prompt, material, record):
        options = dict(max_tokens=2000, temperature=0, **no_thinking_options(self.role.base_url))
        try:
            result = CompletionModel(endpoint=self.role.base_url, key=self.role.api_key,
                model=self.role.model, role="receipt_judge", transport=self.transport,
                timeout=self.timeout, request_options=options).complete_sync(prompt, material)
        except Exception as exc:
            record(dict(stage="receipt_judge", model=self.role.model, state="failed",
                usage=safe_usage(getattr(exc, "usage", None)),
                code=exc.code if isinstance(exc, CompletionModelError) else "receipt_judge_call_failed"))
            raise
        record(dict(stage="receipt_judge", model=result["model"], state="succeeded",
            usage=safe_usage(result["usage"])))
        return result
