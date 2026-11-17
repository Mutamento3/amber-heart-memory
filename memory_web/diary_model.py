"""Configured diary role, never a private model client or implicit fake."""

from .completion_model import CompletionModel, CompletionModelError as DiaryModelError

REFUSAL_MARKERS = ("无法给到", "无法提供", "无法生成", "无法为您", "I cannot", "I'm unable")
MIN_DIARY_LENGTH = 100


class DiaryModel(CompletionModel):
    def __init__(self, settings, *, transport=None):
        super().__init__(
            endpoint=settings.diary_base_url or settings.upstream_base_url,
            key=settings.diary_api_key or settings.upstream_api_key,
            model=settings.diary_model,
            backup=settings.diary_backup_model,
            role="diary",
            timeout=settings.diary_timeout_seconds,
            transport=transport,
        )

    def validate_content(self, content):
        if len(content) < MIN_DIARY_LENGTH or any(marker in content for marker in REFUSAL_MARKERS):
            raise DiaryModelError("invalid_diary_content")

    async def generate(self, prompt, material):
        return await self.complete(prompt, material)
