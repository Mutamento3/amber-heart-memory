"""Monthly generation only; publication and approval are core transactions."""

import logging

from starlette.concurrency import run_in_threadpool

from memory_core.storage.db import connect
from memory_core.lifecycle.topic_summaries import TopicSummaryStore
from memory_core.lifecycle.topic_summary_contract import TopicSummaryFormatError, validate_response


LOG = logging.getLogger("memory_web.daily")


class TopicGenerationFailed(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('主题总结未完成，会保留来源并稍后重试')


class DailyTopicSummaries:
    def __init__(self, settings, models, clock):
        self.settings, self.models, self.clock = settings, models, clock

    async def __call__(self, task, diary_store, tasks, now):
        store = await run_in_threadpool(TopicSummaryStore,self.settings.dsn,task['bot_id'])
        await run_in_threadpool(store.recover,task,now=self.clock())
        plans = await run_in_threadpool(store.plans,task['business_date'],now=self.clock())
        if any(p['state']=='overflow' for p in plans):
            raise TopicGenerationFailed('topic_summary_window_overflow')
        failed_windows = 0
        failure_record_lost = False
        for plan in plans:
            if plan['state']!='ready':
                continue
            batch = None
            try:
                prepared = await run_in_threadpool(store.prepare,task,plan,now=self.clock())
                batch, prompt = prepared['batch'], prepared['prompt']
                if prompt is None:
                    continue

                def record(event):
                    store.record_usage(task,batch['id'],event,now=self.clock())

                for attempt in range(2):
                    raw = await run_in_threadpool(self.models.generate,prompt,record)
                    try:
                        parsed = validate_response(raw,[r['id'] for r in plan['cards']])
                        break
                    except TopicSummaryFormatError:
                        if attempt==1:
                            raise
                await run_in_threadpool(store.publish,task,batch['id'],parsed,now=self.clock())
            except Exception as exc:
                failed_windows += 1
                code = getattr(exc,'code',None) or 'topic_summary_generation_failed'
                LOG.warning("topic_summary window failed space=%s topic=%s start=%s end=%s code=%s",
                    task['bot_id'], plan['topic']['id'], plan['start_month'], plan['end_month'], code)
                if batch is not None:
                    try:
                        await run_in_threadpool(store.fail,task,batch['id'],code,now=self.clock())
                    except Exception as error:
                        failure_record_lost = True
                        LOG.warning("topic_summary could not record failure batch=%s error=%s",
                            batch['id'], type(error).__name__)
        if failure_record_lost:
            # Recover any surviving generating reservations on the same cycle.
            raise TopicGenerationFailed('topic_summary_failure_record_failed')
        # Re-read all batches from this cycle, including ones finished before a retry.
        def counts():
            with connect(self.settings.dsn) as c:
                return c.execute('''SELECT count(*) FILTER (WHERE status='pending') AS pending_count,
                    count(*) FILTER (WHERE status='approved') AS approved_count,
                    count(*) FILTER (WHERE status='empty') AS empty_count
                    FROM memory_core.topic_summary_batches WHERE bot_id=%s AND cycle_date=%s''',
                    (task['bot_id'],task['business_date'])).fetchone()
        result = await run_in_threadpool(counts)
        result['waiting_windows'] = sum(p['state']=='waiting' for p in plans)
        result['failed_windows'] = failed_windows
        await run_in_threadpool(tasks.finish,task,now=self.clock(),result=result)
