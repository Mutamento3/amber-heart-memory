"""Nonblocking per-space scheduling, backed by a cross-worker database lock."""

import asyncio
from datetime import datetime, timezone
import logging

import anyio
from starlette.concurrency import run_in_threadpool

from memory_core.storage.clock import business_day_label
from memory_core.storage.db import connect
from memory_core.daily.summaries import SummaryStore
from memory_core.daily.summary_policy import SummarySettings
from ..models.summary_model import SummaryModel

LOG = logging.getLogger("memory_web.summary")


class SummaryJobs:
    def __init__(self, settings, model=None):
        self.settings = settings
        self.policy = SummarySettings.from_env()
        self.model = model or SummaryModel(settings)
        self.tasks = {}
        self.pending = {}

    def schedule(self, space, turn_id):
        if space in self.tasks:
            self.pending[space] = turn_id
            return
        task = asyncio.create_task(self.run(space, turn_id))
        self.tasks[space] = task

        def finished(done):
            self.tasks.pop(space, None)
            pending = self.pending.pop(space, None)
            if pending is not None:
                self.schedule(space, pending)

        task.add_done_callback(finished)

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()))

    def _day(self, store, turn_id):
        with connect(store.dsn) as c:
            turn = c.execute(
                "SELECT business_date,generation_state FROM memory_core.conversation_turns WHERE id=%s AND bot_id=%s",
                (turn_id, store.space_id),
            ).fetchone()
        if not turn or turn["generation_state"] not in (
            "complete",
            "failed",
            "incomplete",
        ):
            return None
        today = business_day_label(
            datetime.now(timezone.utc), store.settings
        ).isoformat()
        return turn["business_date"] if turn["business_date"] == today else None

    async def run(self, space, turn_id):
        manager = None
        try:
            store = await run_in_threadpool(
                SummaryStore, self.settings.dsn, space, self.policy
            )
            day = await run_in_threadpool(self._day, store, turn_id)
            if day is None:
                return
            manager = store.job_lock()
            acquired = await run_in_threadpool(manager.__enter__)
            while not acquired:
                await run_in_threadpool(manager.__exit__, None, None, None)
                manager = None
                if await run_in_threadpool(self._day, store, turn_id) != day:
                    return
                # The other worker may have checked its last batch just before
                # our reply committed. Recheck after it releases the lock so
                # that completed-round triggers cannot disappear in that gap.
                await asyncio.sleep(0.2)
                manager = store.job_lock()
                acquired = await run_in_threadpool(manager.__enter__)
            while await run_in_threadpool(self._day, store, turn_id) == day:
                plan = await run_in_threadpool(store.next_plan, day)
                if not plan:
                    return
                result = await self.model.summarize(plan["prompt"], plan["material"])
                saved = await run_in_threadpool(store.save, plan, result)
                if saved is None:
                    LOG.warning(
                        "summary_source_changed space=%s turn_id=%s level=%s",
                        space,
                        turn_id,
                        plan["level"],
                    )
                    return
        except Exception as exc:
            # No provider response, prompt, dialogue or secret in diagnostics.
            LOG.warning(
                "summary_failed space=%s turn_id=%s type=%s code=%s",
                space,
                turn_id,
                type(exc).__name__,
                getattr(exc, "code", None) or getattr(exc, "sqlstate", None),
            )
        finally:
            if manager:
                with anyio.CancelScope(shield=True):
                    try:
                        await run_in_threadpool(manager.__exit__, None, None, None)
                    except Exception as exc:
                        LOG.warning(
                            "summary_unlock_failed space=%s type=%s",
                            space,
                            type(exc).__name__,
                        )
