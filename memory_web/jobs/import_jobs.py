"""A restartable pool of import workers using the configured extraction role."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
from threading import Event, Lock
from time import monotonic, sleep

from starlette.concurrency import run_in_threadpool

from memory_core.cards.drafts import (
    IMPORT_RETRY_HTTP_SUFFIXES, MAX_CONCURRENT_STRUCTURES, ExtractionConfigurationChanged,
    MaterialExpired, StructureInProgress,
)
from memory_core.models.http_transport import ModelCallError
from memory_core.imports.imports import IMPORT_APPROVAL_FAILED, ImportStore, import_run_lock
from memory_core.cards.review import ReviewStore
from memory_core.storage.space import RevisionConflict
from ..models.extraction_model import ExtractionModel, safe_usage

LOG = logging.getLogger('memory_web.imports')


class ImportBackoff:
    def __init__(self):
        self.lock = Lock()
        self.generation = 0
        self.delay = 0
        self.next_call = 0
        self.stagger_until = 0

    def wait(self):
        while True:
            with self.lock:
                now = monotonic()
                remaining = self.next_call - now
                if remaining <= 0:
                    if now < self.stagger_until:
                        self.next_call = now + 1
                    return self.generation
            sleep(remaining)

    def limited(self, concurrent_calls):
        with self.lock:
            self.generation += 1
            self.delay = min(60, max(2, self.delay * 2))
            self.next_call = max(self.next_call, monotonic() + self.delay)
            # Space out slots after the cooldown even if the first probe succeeds.
            self.stagger_until = self.next_call + concurrent_calls

    def succeeded(self, generation):
        with self.lock:
            # A response from before the cooldown cannot reset the backoff.
            if generation == self.generation:
                self.delay = 0


class ImportJobs:
    def __init__(self, settings, *, model=None, clock=None, embedder=None, embedding_model='unconfigured'):
        self.settings = settings
        self.model = model or ExtractionModel(settings)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stopping = Event()
        self.wakeup = asyncio.Event()
        self.backoff = ImportBackoff()
        self.embedder, self.embedding_model = embedder, embedding_model
        self.approval_lock = Lock()
        self.task = None

    def run_once(self):
        if not self.model.configured or self.stopping.is_set():
            return False
        with import_run_lock(self.settings.dsn) as guard:
            if guard is None:
                return False
            job = guard.execute("""SELECT id,bot_id FROM memory_core.import_jobs
                WHERE status IN ('queued','running') ORDER BY created_at::timestamptz,id LIMIT 1""").fetchone()
            guard.commit()
            if not job:
                return False
            store = ImportStore(self.settings.dsn, job['bot_id'])
            store.expire(now=self.clock())
            # A previous lock holder may have stopped mid-call. Do this
            # before starting any local workers, never while they own items.
            store.recover_running(job['id'], now=self.clock())
            guard_lock = Lock()

            def check_lock():
                with guard_lock:
                    guard.execute('SELECT 1')
                    guard.commit()

            with ThreadPoolExecutor(max_workers=self.settings.import_concurrency) as pool:
                workers = [pool.submit(self._work, store, job['id'], check_lock)
                           for _ in range(self.settings.import_concurrency)]
                results = [worker.result() for worker in workers]
            return any(results)

    def _work(self, store, identifier, check_lock):
        worked = False
        while not self.stopping.is_set():
            check_lock()
            try:
                item = store.prepare_next(identifier, now=self.clock())
            except (ExtractionConfigurationChanged, MaterialExpired) as error:
                code = self._pause_code(error)
                LOG.warning('import_paused space=%s job=%s code=%s', store.space_id, identifier, code)
                store.fail_job(identifier, code, now=self.clock())
                return True
            if item is None:
                break
            worked = True
            if item.get('skip'):
                if item.get('error_code'):
                    LOG.warning('import_item_failed space=%s job=%s item=%s code=%s',
                        store.space_id, identifier, item['id'], item['error_code'])
                continue
            self._process(store, item, check_lock)
        return worked

    @staticmethod
    def _pause_code(error):
        if isinstance(error, ExtractionConfigurationChanged):
            return 'import_configuration_changed'
        if isinstance(error, MaterialExpired):
            return 'import_material_expired'
        return None

    def _process(self, store, item, check_lock):
        identifier = item['job_id']

        def record(event):
            check_lock()
            safe = dict(item_id=item['id'], stage=event['stage'], model=self.model.role.model,
                state=event['state'], usage=safe_usage(event.get('usage')))
            if event.get('code'):
                safe['code'] = event['code']
                LOG.warning('import_model_failed space=%s job=%s item=%s stage=%s code=%s',
                    store.space_id, identifier, item['id'], event['stage'], event['code'])
            store.record_usage(identifier, safe, now=self.clock())

        class GuardedStage:
            def __init__(self, stage):
                self.stage = stage

            def extract(stage, prompt):
                generation = self.backoff.wait()
                check_lock()
                try:
                    value = stage.stage.extract(prompt)
                except ModelCallError as error:
                    if error.code.endswith(IMPORT_RETRY_HTTP_SUFFIXES):
                        # One material can have several structure calls in
                        # flight while the other workers are still selecting.
                        self.backoff.limited(self.settings.import_concurrency + MAX_CONCURRENT_STRUCTURES - 1)
                    raise
                self.backoff.succeeded(generation)
                return value

        step = 'resume'
        error_code = None
        try:
            store.resume(item)
            step = 'extract'
            result = store.extract(item['extraction_id'],
                selector=GuardedStage(self.model.stage('selection', record)),
                structurer=GuardedStage(self.model.stage('structure', record)),
                clock=self.clock, retry_rate_limit=True, model_timeout=self.settings.extraction_timeout_seconds)
            if item['auto_approve']:
                self._approve_drafts(store, result['drafts'], check_lock)
            if result['status'] != 'complete':
                error_code = 'import_selection_failed' if result['status'] == 'failed' else 'import_structure_failed'
        except StructureInProgress:
            check_lock()
            store.defer_item(item, now=self.clock())
            return
        except Exception as error:
            if isinstance(error, ExtractionConfigurationChanged):
                check_lock()
                if store.defer_item(item, now=self.clock(), require_same_context=True):
                    return
            error_code = self._pause_code(error) or 'import_' + step + '_failed'
            LOG.warning('import_item_failed space=%s job=%s item=%s step=%s type=%s code=%s',
                store.space_id, identifier, item['id'], step, type(error).__name__,
                getattr(error, 'sqlstate', None) or error_code)
        check_lock()
        store.finish_item(item, now=self.clock(), error=error_code)

    def _approve_drafts(self, store, drafts, check_lock):
        review = ReviewStore(self.settings.dsn, store.space_id)
        for draft in drafts:
            if draft['status'] != 'pending' or draft['error_code'] == IMPORT_APPROVAL_FAILED:
                continue
            # Avoid preparing conflicting snapshots of the same new entity in
            # this import. Extraction workers and daily jobs keep their own slots.
            with self.approval_lock:
                check_lock()
                try:
                    review.approve(draft['id'], expected_version=draft['version'],
                        expected_policy_revision=draft['policy_revision'], request_key=f'import-auto:{draft["id"]}',
                        embedder=self.embedder, embedding_model=self.embedding_model, guard=lambda _: check_lock())
                except Exception as exc:
                    check_lock()
                    if isinstance(exc, RevisionConflict) and review.policy()['revision'] != draft['policy_revision']:
                        raise ExtractionConfigurationChanged('导入收录期间配置已改变') from exc
                    LOG.warning('import_approval_failed space=%s draft=%s code=%s',
                        store.space_id, draft['id'], getattr(exc,'code',None) or type(exc).__name__)
                    store.approval_failed(draft)

    async def loop(self):
        while not self.stopping.is_set():
            self.wakeup.clear()
            try:
                if await run_in_threadpool(self.run_once):
                    await asyncio.sleep(0)
                    continue
            except Exception as exc:
                LOG.warning('import_worker_failed type=%s code=%s',type(exc).__name__,getattr(exc,'sqlstate',None))
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        self.stopping.set()
        self.wakeup.set()
        if self.task:
            # Finish the current durable units on graceful shutdown. A killed
            # process leaves running state for the next lock holder.
            await self.task
