"""Latest-only secretary jobs, independently cancellable from chat/daily jobs."""

import asyncio
from datetime import datetime
import logging
from zoneinfo import ZoneInfo

import anyio
from starlette.concurrency import run_in_threadpool

from memory_core.db import connect
from memory_core.recent_notes import RecentNotesStore
from memory_core.recent_note_tidy import TidyPolicy, prepare_request, parse_result, validate_result
from memory_core.recent_note_text import char_count
from .recent_note_model import RecentNoteTidyModel

LOG = logging.getLogger('memory_web.recent_notes')


class RecentNoteTidyJobs:
    def __init__(self, settings, model=None):
        self.settings = settings
        self.model = model or RecentNoteTidyModel(settings)
        self.policy = TidyPolicy(settings.recent_notes_soft_limit, settings.recent_notes_target)
        self.tasks = {}

    def store(self, space):
        return RecentNotesStore(self.settings.dsn, space)

    def recover(self):
        with connect(self.settings.dsn) as c:
            spaces = [row['bot_id'] for row in c.execute("SELECT bot_id FROM memory_core.recent_notes WHERE tidy_job->>'status'='running'")]
        for space in spaces:
            self.store(space).recover()

    async def start(self, space):
        if not self.model.configured:
            raise ValueError('尚未配置秘书整理模型和上游，请先填写 MEMORY_RECENT_NOTES_TIDY_MODEL')
        store = await run_in_threadpool(self.store, space)
        manager = store.tidy_lock()
        acquired = await run_in_threadpool(manager.__enter__)
        handed_off = False
        try:
            if not acquired:
                return (await run_in_threadpool(store.read, self.policy))['tidy_job']
            job, started = await run_in_threadpool(store.begin_tidy, self.policy)
            if started:
                task = asyncio.create_task(self._run(store, job, manager))
                self.tasks[job['id']] = task
                task.add_done_callback(lambda done: self.tasks.pop(job['id'], None))
                handed_off = True
                await asyncio.sleep(0)
            return job
        finally:
            if not handed_off:
                await run_in_threadpool(manager.__exit__, None, None, None)

    async def _run(self, store, job, manager):
        step = 'read_job'
        try:
            latest = (await run_in_threadpool(store.read, self.policy))['tidy_job']
            if latest.get('id')!=job['id'] or latest.get('status')!='running':
                return
            step = 'read_policy'
            policy = await run_in_threadpool(store.policy)
            today = datetime.now(ZoneInfo(store.settings.timezone)).date()
            step = 'prepare_prompt'
            prompt, material, entries = prepare_request(job['base_sections'], self.policy, policy['participants'], today)
            step = 'model'
            result = await self.model.generate(prompt, material)
            step = 'validate'
            validated = validate_result(parse_result(result['content']), entries,
                allow_delete=sum(char_count(value) for value in job['base_sections'].values()) > self.policy.soft_limit)
            values = dict(validated, status='done', error='', model=result.get('model'), usage=result.get('usage'))
            step = 'save_result'
            await self._finish(store, job['id'], values)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await self._finish(store, job['id'], dict(status='failed', error='服务停止，整理中断，请重新整理'))
        except Exception as exc:
            LOG.warning('recent_note_tidy_failed space=%s job=%s step=%s type=%s code=%s', store.space_id, job['id'],
                step, type(exc).__name__, getattr(exc,'code',None) or getattr(exc,'sqlstate',None) or 'tidy_internal_error')
            message = str(exc) if isinstance(exc, ValueError) else '秘书整理失败，请稍后重试'
            await self._finish(store, job['id'], dict(status='failed', error=message))
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await run_in_threadpool(manager.__exit__, None, None, None)
                except Exception as exc:
                    LOG.error('recent_note_tidy_unlock_failed space=%s job=%s type=%s code=%s',
                        store.space_id, job['id'], type(exc).__name__, getattr(exc,'sqlstate',None))

    async def _finish(self, store, identifier, values):
        try:
            return await run_in_threadpool(store.finish_tidy, identifier, values)
        except Exception as exc:
            # Leave the durable running record for restart recovery if storage
            # is unavailable; never expose the DSN/provider exception text.
            LOG.error('recent_note_tidy_save_failed space=%s job=%s type=%s code=%s',
                store.space_id, identifier, type(exc).__name__, getattr(exc,'sqlstate',None))
            return False

    async def cancel(self, space):
        store = await run_in_threadpool(self.store, space)
        job = await run_in_threadpool(store.cancel_tidy)
        task = self.tasks.get(job.get('id'))
        if task and job.get('status')=='cancelled':
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return job

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()))

    async def stop(self):
        for task in list(self.tasks.values()):
            task.cancel()
        await self.drain()
