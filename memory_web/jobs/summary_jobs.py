"""Nonblocking per-space scheduling, backed by a cross-worker database lock."""

import asyncio
from datetime import date, datetime, timedelta, timezone
import logging

import anyio
from starlette.concurrency import run_in_threadpool

from memory_core.storage.clock import business_day_label
from memory_core.storage.db import connect
from memory_core.daily.summaries import SummaryStore
from memory_core.daily.summary_policy import SummarySettings
from memory_core.conversations.conversations import SUMMARY_RETENTION_DAYS
from ..models.summary_model import SummaryModel

LOG = logging.getLogger("memory_web.summary")


class SummaryJobs:
    def __init__(self, settings, model=None):
        self.settings = settings
        self.policy = SummarySettings.from_env()
        self.model = model or SummaryModel(settings)
        self.tasks = {}
        self.pending = {}

    def schedule(self, space, turn_id=None, *, rebuild_day=None, retry_attempt=None):
        if space in self.tasks:
            # Ordinary notifications coalesce. Each day affected by a
            # deletion still needs its own pass through the summary store.
            if retry_attempt is None:
                self.pending.setdefault(space, {})[rebuild_day] = turn_id
            return False
        task = asyncio.create_task(self.run(
            space, turn_id, rebuild_day=rebuild_day, retry_attempt=retry_attempt
        ))
        self.tasks[space] = task

        def finished(done):
            self.tasks.pop(space, None)
            pending = self.pending.get(space)
            if pending:
                day = next(iter(pending))
                identifier = pending.pop(day)
                if not pending:
                    self.pending.pop(space)
                self.schedule(space, identifier, rebuild_day=day)

        task.add_done_callback(finished)
        return True

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

    def status(self, space):
        return SummaryStore(self.settings.dsn, space, self.policy).status()

    async def retry(self, space, attempt_id):
        current = await run_in_threadpool(self.status, space)
        attempt = current["attempt"]
        if (
            current["status"] == "failed" and attempt["id"] == attempt_id
            and self.schedule(space, attempt["turn_id"], retry_attempt=attempt)
        ):
            return dict(current, status="generating")
        return current

    async def run(self, space, turn_id, *, rebuild_day=None, retry_attempt=None):
        manager = None
        store = None
        attempt = None
        try:
            store = await run_in_threadpool(
                SummaryStore, self.settings.dsn, space, self.policy
            )
            async def work_day():
                requested_day = retry_attempt["business_date"] if retry_attempt else rebuild_day
                if requested_day is None:
                    return await run_in_threadpool(self._day, store, turn_id)
                today = business_day_label(datetime.now(timezone.utc), store.settings)
                if date.fromisoformat(requested_day) >= today - timedelta(days=SUMMARY_RETENTION_DAYS):
                    return requested_day
                return None

            day = await work_day()
            if day is None:
                return
            manager = store.job_lock()
            acquired = await run_in_threadpool(manager.__enter__)
            while not acquired:
                await run_in_threadpool(manager.__exit__, None, None, None)
                manager = None
                if await work_day() != day:
                    return
                # The other worker may have checked its last batch just before
                # our reply committed. Recheck after it releases the lock so
                # that completed-round triggers cannot disappear in that gap.
                await asyncio.sleep(0.2)
                manager = store.job_lock()
                acquired = await run_in_threadpool(manager.__enter__)
            if retry_attempt:
                current = await run_in_threadpool(store.status)
                # A queued retry must not run after a different worker has handled it.
                if not current["attempt"] or current["attempt"]["id"] != retry_attempt["id"]:
                    return
            rebuild = retry_attempt["rebuild"] if retry_attempt else rebuild_day is not None
            if retry_attempt:
                attempt = await run_in_threadpool(
                    store.start_attempt, day, turn_id=turn_id, rebuild=rebuild
                )
            saved_any = False
            while await work_day() == day:
                plan = await run_in_threadpool(store.next_plan, day, rebuild=rebuild)
                if not plan:
                    break
                if attempt is None:
                    attempt = await run_in_threadpool(
                        store.start_attempt, day, turn_id=turn_id, rebuild=rebuild
                    )
                result = await self.model.summarize(plan["prompt"], plan["material"])
                saved = await run_in_threadpool(store.save, plan, result)
                if saved is None:
                    LOG.warning(
                        "summary_source_changed space=%s turn_id=%s level=%s",
                        space,
                        turn_id,
                        plan["level"],
                    )
                    await run_in_threadpool(store.finish_attempt, attempt, "failed", error="source_changed")
                    return
                saved_any = True
            if attempt:
                # The source of the failed job may have been removed before retry.
                await run_in_threadpool(store.finish_attempt, attempt, "success" if saved_any else "idle")
        except Exception as exc:
            # No provider response, prompt, dialogue or secret in diagnostics.
            LOG.warning(
                "summary_failed space=%s turn_id=%s type=%s code=%s",
                space,
                turn_id,
                type(exc).__name__,
                getattr(exc, "code", None) or getattr(exc, "sqlstate", None),
            )
            if attempt:
                try:
                    await run_in_threadpool(store.finish_attempt, attempt, "failed", error="summary_failed")
                except Exception as status_error:
                    LOG.warning("summary_status_failed space=%s type=%s", space, type(status_error).__name__)
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
