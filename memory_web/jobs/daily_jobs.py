"""In-service daily handlers with durable keys, restart recovery and retry."""

import asyncio
from datetime import date, datetime, timezone
import logging
import re

import anyio
from starlette.concurrency import run_in_threadpool

from memory_core.daily.daily_policy import DailySettings
from memory_core.daily.daily_tasks import DailyTasks
from memory_core.storage.db import connect
from memory_core.daily.diaries import DiaryStore
from ..models.diary_model import DiaryModel
from ..models.extraction_model import ExtractionModel
from .daily_extraction import DailyExtraction
from .daily_approval import DailyApproval
from .daily_receipts import DailyReceipts
from ..models.receipt_judge_model import ReceiptJudgeModel
from memory_core.cards.review_policy import ApprovalSettings
from memory_core.lifecycle.archive_policy import ArchiveSettings
from memory_core.lifecycle.archives import ArchiveStore
from memory_core.storage.schema import DELAYED_APPROVAL_VERSION, ARCHIVE_VERSION, TOPIC_SUMMARY_VERSION, RECENT_NOTES_VERSION
from memory_core.storage.schema import HOOK_VERSION
from memory_core.conversations.hook_turns import HookTurns
from memory_core.bookmarks.recent_notes import RecentNotesStore
from ..models.topic_summary_model import TopicSummaryModel
from .daily_topic_summaries import DailyTopicSummaries

LOG = logging.getLogger("memory_web.daily")


class DailyJobs:

    def __init__(
        self,
        settings,
        core,
        *,
        model=None,
        enabled=True,
        clock=None,
        extraction_models=None,
        embedder=None,
        embedding_model="unconfigured",
        extraction_enabled=True,
        topic_summary_models=None,
        receipt_judge_models=None,
    ):
        self.settings, self.core = settings, core
        self.policy = DailySettings.from_env()
        self.approval_policy = ApprovalSettings.from_env()
        self.archive_policy = ArchiveSettings.from_env()
        with connect(settings.dsn) as c:
            version = c.execute("SELECT version FROM memory_core.installation").fetchone()["version"]
            self.approval_enabled = version >= DELAYED_APPROVAL_VERSION
            self.archive_enabled = version >= ARCHIVE_VERSION
            self.topic_summary_enabled = version >= TOPIC_SUMMARY_VERSION
            self.recent_notes_enabled = version >= RECENT_NOTES_VERSION
            self.hooks_enabled = version >= HOOK_VERSION
        self.model = model or DiaryModel(settings)
        self.enabled = enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.extraction_models = extraction_models or ExtractionModel(settings)
        self.extraction_enabled = extraction_enabled
        self.topic_summary_models = topic_summary_models or TopicSummaryModel(settings)
        self.receipt_judge_models = receipt_judge_models or ReceiptJudgeModel(settings)
        self.tasks = {}
        self.stopping = asyncio.Event()
        self.loop_task = None
        # Handler order is the daily sequence; cleanup is always dispatched last.
        self.handlers = {
            **({'recent_notes': self.recent_notes} if self.recent_notes_enabled else {}),
            "diary": self.diary,
            "extraction": DailyExtraction(
                settings, self.extraction_models, self.clock
            ),
            # Before approval and archive, so judged use counts toward today's archive clock.
            "receipts": DailyReceipts(settings, core, self.receipt_judge_models, self.clock),
            **({"topic_summaries": DailyTopicSummaries(settings,self.topic_summary_models,self.clock)} if self.topic_summary_enabled else {}),
            **({"approval":DailyApproval(settings,self.approval_policy,self.policy,core,embedder,embedding_model,self.clock)} if self.approval_enabled else {}),
            **({"archive": self.archive} if self.archive_enabled else {}),
            "cleanup": self.cleanup,
        }
        if enabled:
            from memory_core.storage.clock import business_day_label

            self.policy.scheduled_at(
                business_day_label(datetime.now(timezone.utc), core), core
            )

    def spaces(self):
        with connect(self.settings.dsn) as c:
            bookmarks = ("OR EXISTS (SELECT 1 FROM memory_core.moment_bookmarks b WHERE b.bot_id=p.bot_id)"
                         if self.recent_notes_enabled else '')
            return [
                r["bot_id"]
                for r in c.execute("""SELECT bot_id FROM memory_core.space_policies p
                WHERE EXISTS (SELECT 1 FROM memory_core.conversation_turns t WHERE t.bot_id=p.bot_id)
                OR EXISTS (SELECT 1 FROM memory_core.daily_tasks j WHERE j.bot_id=p.bot_id AND j.state IN ('running','failed'))
                OR EXISTS (SELECT 1 FROM memory_core.pending_memories d WHERE d.bot_id=p.bot_id AND d.status='pending')
                OR (%s AND EXISTS (SELECT 1 FROM memory_core.memory_items m WHERE m.bot_id=p.bot_id AND m.archive_reason IS NULL))
                """ + bookmarks + ' ORDER BY bot_id', (self.archive_enabled,))
            ]

    def work(self, space, now):
        start, end = self.policy.diary_range(now, self.core)
        card_start, card_end = self.policy.extraction_range(now, self.core)
        cards_enabled = self.extraction_enabled and self.extraction_models.configured
        first = min(start, card_start) if cards_enabled else start
        due = self.policy.due_date(now, self.core).isoformat()
        with connect(self.settings.dsn) as c:
            days = {
                r["business_date"]
                for r in c.execute(
                    """SELECT DISTINCT business_date FROM memory_core.conversation_turns
                WHERE bot_id=%s AND history_state='current' AND business_date BETWEEN %s AND %s""",
                    (space, first.isoformat(), end.isoformat()),
                )
            }
            # Record the just-finished day as skipped if it had no dialogue.
            days.add(end.isoformat())
            recorded = c.execute(
                """SELECT task_kind,business_date,state,retry_at FROM memory_core.daily_tasks WHERE bot_id=%s
                AND business_date<=%s AND (business_date>=%s OR state IN ('running','failed'))""",
                (space, due, first.isoformat()),
            ).fetchall()
        jobs = {("diary", d) for d in days if start.isoformat() <= d <= end.isoformat()}
        if cards_enabled:
            jobs.update(
                ("extraction", d)
                for d in days
                if card_start.isoformat() <= d <= card_end.isoformat()
            )
            jobs.add(("extraction", card_end.isoformat()))
        receipts_enabled = self.receipt_judge_models.configured
        if receipts_enabled:
            jobs.update(("receipts", d) for d in days if start.isoformat() <= d <= end.isoformat())
        summaries_enabled = self.topic_summary_enabled and self.topic_summary_models.configured
        if summaries_enabled and date.fromisoformat(due).day == 3:
            jobs.add(("topic_summaries",due))
        if self.approval_enabled:
            jobs.add(("approval",due))
        if self.archive_enabled:
            jobs.add(("archive", due))
        if self.recent_notes_enabled:
            jobs.add(('recent_notes', due))
        jobs.add(("cleanup", due))
        blocked = set()
        for row in recorded:
            key = (row["task_kind"], row["business_date"])
            if row["state"] in ("done", "skipped") or (
                row["state"] == "failed"
                and datetime.fromisoformat(row["retry_at"]) > now
            ):
                blocked.add(key)
                continue
            if summaries_enabled and row['task_kind']=='topic_summaries':
                jobs.add((row['task_kind'],row['business_date']))
            elif row["task_kind"] in ("approval", "archive", "recent_notes") and row["task_kind"] in self.handlers and row["business_date"] == due:
                jobs.add((row["task_kind"],row["business_date"]))
            elif row["task_kind"] == "cleanup" or (
                row["task_kind"] in ("diary", "receipts")
                and (row["task_kind"] == "diary" or receipts_enabled)
                and start.isoformat() <= row["business_date"] <= end.isoformat()
            ):
                jobs.add((row["task_kind"], row["business_date"]))
            elif (
                cards_enabled
                and row["task_kind"] == "extraction"
                and card_start.isoformat()
                <= row["business_date"]
                <= card_end.isoformat()
            ):
                jobs.add((row["task_kind"], row["business_date"]))
        # This is only a cheap dispatch precheck; claim still owns the atomic
        # final check, so concurrent workers cannot turn it into double work.
        order = {kind: index for index, kind in enumerate(self.handlers)}
        return sorted(
            jobs - blocked, key=lambda job: (job[0] == "cleanup", job[0] != 'recent_notes', job[1], order[job[0]])
        )

    async def diary(self, task, store, tasks, now):
        existing = await run_in_threadpool(store.get, task["business_date"])
        if existing:
            await run_in_threadpool(
                tasks.finish,
                task,
                now=now,
                result=dict(diary_id=existing["id"], existing=True),
            )
            return
        plan = await run_in_threadpool(store.plan, task["business_date"], now=now)
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
            await run_in_threadpool(tasks.fail, task, plan["reason"], now=now)
            return
        result = await self.model.generate(plan["prompt"], plan["material"])
        # Use actual completion time for retention checks. Explicit tick clocks
        # in acceptance still advance only when the test asks them to.
        finished = self.clock()
        await run_in_threadpool(store.publish, plan, result, task, now=finished)

    async def archive(self, task, store, tasks, now):
        archive = await run_in_threadpool(ArchiveStore, self.settings.dsn, task['bot_id'], self.archive_policy)
        await run_in_threadpool(archive.archive_task, task, now=self.clock())

    async def recent_notes(self, task, store, tasks, now):
        notes = await run_in_threadpool(RecentNotesStore, self.settings.dsn, task['bot_id'])
        await run_in_threadpool(notes.absorb, task, now=self.clock())

    async def cleanup(self, task, store, tasks, now):
        count = await run_in_threadpool(store.expire, now=now)
        imports=0
        from memory_core.storage.schema import IMPORT_VERSION
        if store.schema_version>=IMPORT_VERSION:
            from memory_core.imports.imports import ImportStore
            imported=await run_in_threadpool(ImportStore,self.settings.dsn,task['bot_id'])
            imports=await run_in_threadpool(imported.expire,now=now)
        await run_in_threadpool(
            tasks.finish, task, now=now, result=dict(expired_turns=count,expired_import_materials=imports)
        )

    async def run_space(self, space, now):
        manager = None
        task = None
        try:
            tasks = await run_in_threadpool(DailyTasks, self.settings.dsn, space)
            store = await run_in_threadpool(DiaryStore, self.settings.dsn, space)
            manager = tasks.run_lock()
            acquired = await run_in_threadpool(manager.__enter__)
            if not acquired:
                return
            for kind, day in await run_in_threadpool(self.work, space, now):
                task = await run_in_threadpool(tasks.claim, kind, day, now=now)
                if not task:
                    continue
                try:
                    await self.handlers[kind](task, store, tasks, now)
                except Exception as exc:
                    code = (
                        getattr(exc, "code", None)
                        or getattr(exc, "sqlstate", None)
                        or "daily_handler_failed"
                    )
                    if not isinstance(code, str) or not re.fullmatch(
                        r"[a-zA-Z0-9_:-]{1,100}", code
                    ):
                        code = "daily_handler_failed"
                    LOG.warning(
                        "daily_failed space=%s kind=%s date=%s attempt=%s type=%s code=%s",
                        space,
                        kind,
                        day,
                        task["attempts"],
                        type(exc).__name__,
                        code,
                    )
                    try:
                        await run_in_threadpool(tasks.fail, task, code, now=self.clock())
                    except Exception as error:
                        LOG.warning(
                            "daily_status_failed space=%s kind=%s date=%s type=%s",
                            space,
                            kind,
                            day,
                            type(error).__name__,
                        )
        except Exception as exc:
            LOG.warning(
                "daily_dispatch_failed space=%s type=%s code=%s",
                space,
                type(exc).__name__,
                getattr(exc, "sqlstate", None),
            )
        finally:
            if manager:
                with anyio.CancelScope(shield=True):
                    try:
                        await run_in_threadpool(manager.__exit__, None, None, None)
                    except Exception as exc:
                        LOG.warning(
                            "daily_unlock_failed space=%s type=%s",
                            space,
                            type(exc).__name__,
                        )

    async def tick(self, *, now=None):
        if not self.enabled:
            return
        now = now or self.clock()
        # Settle abandoned tools even when only management pages are enabled.
        from memory_core.conversations.conversations import ConversationStore

        await run_in_threadpool(
            ConversationStore.expire_leases, self.settings.dsn, now=now
        )
        if self.hooks_enabled:
            await run_in_threadpool(HookTurns.expire_pending, self.settings.dsn, self.core, now)
        for space in await run_in_threadpool(self.spaces):
            if space not in self.tasks:
                task = asyncio.create_task(self.run_space(space, now))
                self.tasks[space] = task
                task.add_done_callback(
                    lambda done, space=space: self.tasks.pop(space, None)
                )

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()))

    async def loop(self):
        while not self.stopping.is_set():
            try:
                await self.tick()
            except Exception as exc:
                LOG.warning(
                    "daily_poll_failed type=%s code=%s",
                    type(exc).__name__,
                    getattr(exc, "sqlstate", None),
                )
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        if self.enabled:
            self.loop_task = asyncio.create_task(self.loop())

    async def stop(self):
        self.stopping.set()
        if self.loop_task:
            await self.loop_task
        await self.drain()
