"""Hosted assembly and one deadline across the existing recall pipeline."""

import asyncio
from functools import partial
from time import monotonic

import anyio
from starlette.concurrency import run_in_threadpool

from memory_core.conversations.conversation_context import content_parts
from memory_core.recall.recall import RecallService
from memory_core.recall.recall_config import RecallSettings
from memory_core.recall.search_store import SearchStore
from memory_core.daily.summary_policy import SummarySettings
from memory_core.daily.daily_policy import DailySettings
from memory_core.models.http_transport import call_deadline


class MissingRecallModels:
    """Explicit failure, never silently load private clients or fake defaults."""

    def __getattr__(self, name):
        def missing(*args, **kwargs):
            raise RuntimeError("recall_models_unconfigured")

        return missing


class DeadlineModels:
    def __init__(self, models, deadline, step_timeout):
        self.models, self.deadline = models, deadline
        self.step_timeout = step_timeout

    def __getattr__(self, name):
        method = getattr(self.models, name)
        if name == "prepare_rerank_query":
            return method

        def call(*args, **kwargs):
            return anyio.from_thread.run(self._run, partial(method, *args, **kwargs))

        return call

    async def _run(self, method):
        started = monotonic()
        step_deadline = min(started + self.step_timeout, self.deadline)
        remaining = step_deadline - started
        if remaining <= 0:
            raise TimeoutError("recall_deadline")
        # HTTP adapters also enforce the deadline across network phases. The
        # outer wait bounds other synchronous adapters that ignore it.
        with call_deadline(step_deadline), anyio.fail_after(remaining):
            return await anyio.to_thread.run_sync(method, abandon_on_cancel=True)


class HostedContext:
    def __init__(self, settings, models=None):
        self.settings = settings
        self.models = models or MissingRecallModels()
        self.recall_settings = RecallSettings.from_env()
        self.summary_settings = SummarySettings.from_env()
        self.daily_settings = DailySettings.from_env()
        self.recall_workers = None

    async def prepare(self, store, record, body):
        source = await run_in_threadpool(
            store.history_input,
            record,
            previous_messages=self.settings.previous_day_messages,
            max_history_chars=self.summary_settings.max_history_chars,
            recent_diary_count=self.daily_settings.recent_diaries,
        )
        query = "".join(
            p["text"]
            for p in content_parts(body["messages"][-1].get("content"))
            if p["type"] == "text"
        ).strip()
        snapshot = {"status": "skipped_image", "cards": []}
        if query:
            started = monotonic()
            deadline = started + self.settings.recall_timeout_seconds
            best = {"status": "timeout", "cards": []}
            if self.recall_workers is None:
                # Recall workers wait for model workers, so they cannot share
                # the same capacity limiter.
                self.recall_workers = anyio.CapacityLimiter(
                    anyio.to_thread.current_default_thread_limiter().total_tokens
                )

            def check_time():
                if monotonic() >= deadline:
                    raise TimeoutError("recall_deadline")

            def capture(cards, result):
                stats = result["retrieval_stats"]
                return dict(
                    status=stats.get("status", "ok"),
                    cards=cards,
                    errors=dict(stats.get("errors", {})),
                    ordering=dict(result["ordering_stats"]),
                    selection=dict(result["selection_stats"]),
                )

            def save_progress(rows, result):
                nonlocal best
                # These rows have already been hydrated from the scoped store.
                # Keep an independent snapshot in case the final reread stalls.
                cards = [
                    {key: row[key] for key in (
                        "memory_id", "title", "content", "source_date", "item_kind", "experience_mode"
                    )} | {"number": number}
                    for number, row in enumerate(rows, 1)
                ]
                snapshot = capture(cards, result)
                check_time()
                best = snapshot

            def recall():
                check_time()
                models = DeadlineModels(self.models, deadline, self.settings.recall_models.timeout_seconds)
                service = RecallService(
                    SearchStore(store.dsn, store.space_id), self.recall_settings
                )
                check_time()
                result = service.recall(
                    query,
                    embedder=models,
                    cleaner=models,
                    reranker=models,
                    selector=models,
                    messages=source["history"],
                    check_time=check_time,
                    on_progress=save_progress,
                )
                check_time()
                cards = store.recall_cards(result)
                check_time()
                return capture(cards, result)

            try:
                # Abandoned synchronous clients cannot delay the chat. The
                # deadline wrapper prevents further model stages after timeout;
                # no abandoned worker writes snapshots or counts.
                snapshot = await asyncio.wait_for(
                    anyio.to_thread.run_sync(recall, abandon_on_cancel=True, limiter=self.recall_workers),
                    timeout=deadline - monotonic(),
                )
            except TimeoutError:
                snapshot = dict(best, status="timeout")
            except Exception as exc:
                snapshot = {
                    "status": "failed",
                    "cards": [],
                    "error_type": type(exc).__name__,
                }
            snapshot["elapsed_ms"] = round((monotonic() - started) * 1000)
        return await run_in_threadpool(
            store.save_context, record, body, source, snapshot
        )
