"""An explicit asynchronous fake summary role with controllable failure/delay."""

import asyncio
import threading

from memory_web.models.summary_model import SummaryModelError


class SyntheticSummaryModel:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    async def summarize(self, prompt, material):
        self.calls.append((prompt, material))
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        if self.fail:
            raise SummaryModelError("synthetic_summary_failure")
        return dict(
            content="合成摘要" + str(len(self.calls)),
            model="synthetic-summary",
            usage={"total_tokens": 3},
        )
