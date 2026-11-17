"""Nightly receipt judging for one finished business day."""

from starlette.concurrency import run_in_threadpool

from memory_core.daily.receipt_judge import PROMPT, ReceiptJudgeFormatError, ReceiptJudgeStore, batches, material, parse


class ReceiptJudgeFailed(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__("回执补判未完成，会在稍后重试")


class DailyReceipts:
    def __init__(self, settings, core, models, clock):
        self.settings, self.core, self.models, self.clock = settings, core, models, clock

    async def __call__(self, task, diary_store, tasks, now):
        store = ReceiptJudgeStore(self.settings.dsn, task["bot_id"])
        rounds = await run_in_threadpool(store.candidates, task["business_date"])
        totals = dict(candidates=len(rounds), judged=0, judged_unused=0, left_unreported=0)

        def record(event):
            tasks.record_usage(task, event, now=self.clock())

        for batch in batches(rounds):
            try:
                for attempt in range(2):
                    result = await run_in_threadpool(self.models.judge, PROMPT, material(batch), record)
                    try:
                        judged = parse(result["content"], batch)
                        break
                    except ReceiptJudgeFormatError:
                        if attempt == 1:
                            raise
            except Exception as exc:
                raise ReceiptJudgeFailed(getattr(exc, "code", None) or "receipt_judge_failed") from None
            counts = await run_in_threadpool(store.apply, batch, judged,
                model=result["model"], now=self.clock(), core=self.core)
            totals["judged"] += counts["judged"]
            totals["judged_unused"] += counts["judged_unused"]
            totals["left_unreported"] += len(batch) - counts["judged"] - counts["judged_unused"]
            await run_in_threadpool(tasks.progress, task, totals)
        await run_in_threadpool(tasks.finish, task, now=self.clock(), result=totals)
