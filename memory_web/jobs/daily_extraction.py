"""Daily orchestration only; core draft/review transactions own ingestion."""

from starlette.concurrency import run_in_threadpool

from memory_core.cards.daily_cards import (
    DailyCardStore,
    terminal_drafts,
)
from memory_core.storage.space import RevisionConflict


class ExtractionFailed(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DailyExtraction:
    def __init__(self, settings, model, clock):
        self.settings, self.model, self.clock = settings, model, clock

    async def __call__(self, task, diary_store, tasks, now):
        store = await run_in_threadpool(
            DailyCardStore, self.settings.dsn, task["bot_id"]
        )
        plan = await run_in_threadpool(store.prepare, task, now=now)
        if plan["status"] == "skip":
            await run_in_threadpool(
                tasks.finish,
                task,
                now=now,
                state="skipped",
                result=dict(reason=plan["reason"]),
            )
            return
        if plan["status"] == "retry":
            raise ExtractionFailed(plan["reason"])
        try:
            workflow = await run_in_threadpool(store.resume, plan)
        except RevisionConflict:
            raise ExtractionFailed("extraction_configuration_changed") from None

        def record(event):
            tasks.record_usage(task, event, now=self.clock())

        outcome = await run_in_threadpool(workflow.extraction, plan["extraction_id"])
        if not terminal_drafts(outcome):
            outcome = await run_in_threadpool(
                workflow.extract,
                plan["extraction_id"],
                selector=self.model.stage("selection", record),
                structurer=self.model.stage("structure", record),
                clock=self.clock,
                model_timeout=self.settings.extraction_timeout_seconds,
            )
        if outcome["status"] != "complete" and not terminal_drafts(outcome):
            code = (
                "extraction_selection_failed"
                if outcome["status"] == "failed"
                else "extraction_structure_failed"
            )
            raise ExtractionFailed(code)
        await run_in_threadpool(store.check, task, plan, now=self.clock())
        counts = await run_in_threadpool(store.counts, plan["extraction_ids"])
        await run_in_threadpool(tasks.finish, task, now=self.clock(), result=counts)
