"""Configured summary role sharing the public text-completion transport."""

from .completion_model import CompletionModel, CompletionModelError as SummaryModelError


class SummaryModel(CompletionModel):
    attempts_per_model = 2

    def __init__(self, settings, *, transport=None):
        super().__init__(
            endpoint=settings.summary_base_url or settings.upstream_base_url,
            key=settings.summary_api_key or settings.upstream_api_key,
            model=settings.summary_model,
            backup=settings.summary_backup_model,
            role="summary",
            timeout=settings.summary_timeout_seconds,
            transport=transport,
        )

    async def summarize(self, prompt, material):
        return await self.complete(prompt, material)
