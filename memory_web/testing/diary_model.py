"""Explicit fake diary writer; never loaded by normal startup."""

from .summary_model import SyntheticSummaryModel


class SyntheticDiaryModel(SyntheticSummaryModel):
    async def generate(self, prompt, material):
        result = await self.summarize(prompt, material)
        result["content"] = "# 助手的日记\n合成日记：我们聊了当天的安排和心情。"
        result["model"] = "synthetic-diary"
        return result
