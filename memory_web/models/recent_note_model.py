"""Explicit secretary model role using the existing completion transport."""

from urllib.parse import urlsplit

from .completion_model import CompletionModel


class RecentNoteTidyModel(CompletionModel):
    def __init__(self, settings, *, transport=None):
        endpoint = settings.recent_notes_tidy_base_url or settings.upstream_base_url
        host = urlsplit(endpoint or '').hostname
        options = dict(max_tokens=32000)
        if host == 'openrouter.ai':
            options['reasoning'] = {'effort': 'medium'}
        elif host == 'api.deepseek.com':
            options.update(thinking={'type': 'enabled'}, reasoning_effort='medium')
        elif host in ('api.siliconflow.cn', 'api.siliconflow.com'):
            options.update(enable_thinking=True, reasoning_effort='medium')
        elif host == 'api.openai.com':
            options.pop('max_tokens')
            options.update(max_completion_tokens=32000, reasoning_effort='medium')
        super().__init__(endpoint=endpoint,
            key=settings.recent_notes_tidy_api_key or settings.upstream_api_key,
            model=settings.recent_notes_tidy_model, role='recent_notes_tidy',
            timeout=settings.recent_notes_tidy_timeout_seconds, transport=transport,
            request_options=options)

    @property
    def configured(self):
        return bool(self.models and self.endpoint and self.key)

    async def generate(self, prompt, material):
        return await self.complete(prompt, material)
