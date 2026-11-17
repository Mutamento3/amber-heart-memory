"""Explicit topic-summary role; no model is selected automatically."""

from memory_core.completion_model import CompletionModel, CompletionModelError
from memory_core.model_config import ModelRole
from .extraction_model import safe_usage


class TopicSummaryModel:
    def __init__(self, settings, *, transport=None, fixed_models=None):
        self.role = ModelRole(settings.topic_summary_model, settings.topic_summary_base_url,
            settings.topic_summary_api_key).resolve(settings.upstream_base_url, settings.upstream_api_key)
        self.transport, self.fixed_models = transport, fixed_models
        self.timeout = settings.topic_summary_timeout_seconds

    @property
    def configured(self):
        return bool(self.role.model and (self.fixed_models or self.role.configured))

    def generate(self, prompt, record):
        try:
            if self.fixed_models is not None:
                result = dict(content=self.fixed_models.extract(prompt),model=self.role.model,usage=None)
            else:
                result = CompletionModel(endpoint=self.role.base_url,key=self.role.api_key,model=self.role.model,
                    role='topic_summary',transport=self.transport,timeout=self.timeout,
                    request_options=dict(temperature=0,max_tokens=12000)).complete_sync(prompt)
        except Exception as exc:
            record(dict(stage='topic_summary',model=self.role.model,state='failed',
                usage=safe_usage(getattr(exc,'usage',None)),
                code=exc.code if isinstance(exc,CompletionModelError) else 'topic_summary_call_failed'))
            raise
        record(dict(stage='topic_summary',model=result['model'],state='succeeded',usage=safe_usage(result['usage'])))
        return result['content']
