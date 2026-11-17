"""Explicit two-step chat role; existing prompts and parsers stay in the core."""

from memory_core.models.completion_model import CompletionModel, CompletionModelError, json_object_options
from memory_core.models.model_config import ModelRole


def safe_usage(value):
    """Keep numeric billing metadata, never arbitrary provider text."""
    if not isinstance(value, dict):
        return None
    fields = {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cost",
        "cached_tokens",
        "audio_tokens",
        "reasoning_tokens",
        "accepted_prediction_tokens",
        "rejected_prediction_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "upstream_inference_cost",
    }
    groups = {
        "prompt_tokens_details",
        "completion_tokens_details",
        "input_tokens_details",
        "output_tokens_details",
        "cost_details",
    }
    import math

    result = {}
    for key, item in value.items():
        if (
            key in fields
            and type(item) in (int, float)
            and math.isfinite(item)
            and item >= 0
        ):
            result[key] = item
        elif key in groups and isinstance(item, dict):
            result[key] = safe_usage(item)
    return result or None


class ExtractionModel:
    def __init__(self, settings, *, transport=None, fixed_models=None):
        self.role = ModelRole(
            settings.extraction_model,
            settings.extraction_base_url,
            settings.extraction_api_key,
        ).resolve(settings.upstream_base_url, settings.upstream_api_key)
        self.transport = transport
        self.timeout = settings.extraction_timeout_seconds
        self.fixed_models = fixed_models

    @property
    def configured(self):
        return bool(self.role.model and (self.fixed_models or self.role.configured))

    def stage(self, name, record_call):
        if name not in ("selection", "structure"):
            raise ValueError("无效的抽卡步骤")
        return ExtractionStage(self, name, record_call)


class ExtractionStage:
    def __init__(self, owner, stage, record_call):
        self.owner, self.name, self.record = owner, stage, record_call

    def extract(self, prompt):
        owner = self.owner
        try:
            if owner.fixed_models is not None:
                value = dict(
                    content=owner.fixed_models.extract(prompt),
                    model=owner.role.model,
                    usage=None,
                )
            else:
                model = CompletionModel(
                    endpoint=owner.role.base_url,
                    key=owner.role.api_key,
                    model=owner.role.model,
                    role="extraction_" + self.name,
                    timeout=owner.timeout,
                    transport=owner.transport,
                    # Same as the self-hosted extractor: malformed JSON wastes a retry.
                    request_options=json_object_options(owner.role.base_url),
                )
                value = model.complete_sync(prompt)
        except Exception as exc:
            self.record(
                dict(
                    stage=self.name,
                    model=owner.role.model,
                    usage=safe_usage(getattr(exc, "usage", None)),
                    state="failed",
                    code=(
                        exc.code
                        if isinstance(exc, CompletionModelError)
                        else "extraction_" + self.name + "_call_failed"
                    ),
                )
            )
            raise
        self.record(
            dict(
                stage=self.name,
                model=value["model"],
                usage=safe_usage(value["usage"]),
                state="succeeded",
            )
        )
        return value["content"]
