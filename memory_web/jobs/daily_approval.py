"""A once-per-day sweep independent of extraction model configuration."""

from datetime import date

from starlette.concurrency import run_in_threadpool

from memory_core.storage.schema import TOPIC_SUMMARY_VERSION
from memory_core.lifecycle.topic_summaries import TopicSummaryStore

from memory_core.cards.daily_cards import CardSourceChanged
from memory_core.cards.pending_approval import PendingApprovalStore, ApprovalNotDue
from memory_core.storage.space import RevisionConflict


class AutomaticApprovalFailed(ValueError):
    code='pending_auto_approval_failed'


class DailyApproval:
    def __init__(self,settings,policy,daily,core,embedder,embedding_model,clock):
        self.settings,self.policy,self.embedder,self.embedding_model,self.clock=settings,policy,embedder,embedding_model,clock
        self.daily,self.core=daily,core

    async def __call__(self,task,diary_store,tasks,now):
        store=await run_in_threadpool(PendingApprovalStore,self.settings.dsn,task['bot_id'],self.policy)
        # Due by business day: drafts made on day D enter in the run for D + delay_days.
        cutoff=self.policy.ready_before(date.fromisoformat(task['business_date']),self.core)
        due=await run_in_threadpool(store.due,cutoff)
        previous=task.get('result') or {}
        result=dict(approved=previous.get('approved',0),duplicates=previous.get('duplicates',0),failed=[],manual=[])
        for row in due:
            try:
                plan=await run_in_threadpool(store.plan,row['id'])
                if self.embedder is None:
                    raise ValueError('approval_embedding_unconfigured')
                draft=plan['draft']
                saved=await run_in_threadpool(store.approve,draft['id'],expected_version=draft['version'],
                    expected_policy_revision=draft['policy_revision'],request_key='pending-auto:'+str(draft['id']),
                    embedder=self.embedder,embedding_model=self.embedding_model,
                    guard=lambda c:store.guard(c,task,plan,now=self.clock(),due_at=cutoff))
                if saved['status']=='approved':
                    result['approved']+=1
            except ApprovalNotDue:
                continue
            except (RevisionConflict,CardSourceChanged):
                # Settings or the day's dialogue changed after extraction. Retrying
                # cannot fix that; the draft waits for a person. Re-checked next day.
                await run_in_threadpool(store.require_manual,row['id'])
                result['manual'].append(row['id'])
            except Exception as exc:
                if await run_in_threadpool(store.still_due,row['id'],cutoff):
                    result['failed'].append(dict(id=row['id'],code=getattr(exc,'code',None) or type(exc).__name__))
            await run_in_threadpool(tasks.progress,task,result)
        if store.schema_version >= TOPIC_SUMMARY_VERSION:
            summaries = await run_in_threadpool(TopicSummaryStore,self.settings.dsn,task['bot_id'])
            for row in await run_in_threadpool(summaries.due,cutoff,self.policy):
                try:
                    saved = await run_in_threadpool(summaries.approve,row['id'],expected_version=row['version'],
                        expected_policy_revision=row['policy_revision'],request_key='summary-auto:'+str(row['id']),
                        embedder=self.embedder,now=self.clock(),
                        guard=lambda c: summaries.guard_auto(c,task,row['id'],cutoff=cutoff,approval_policy=self.policy))
                    if saved['status']=='approved':
                        result['approved']+=1
                except ApprovalNotDue:
                    continue
                except Exception as exc:
                    if await run_in_threadpool(summaries.still_due,row['id'],cutoff,self.policy):
                        result['failed'].append(dict(id=row['id'],item_kind='topic_summary',
                            code=getattr(exc,'code',None) or type(exc).__name__))
                await run_in_threadpool(tasks.progress,task,result)
        if result['failed']:
            raise AutomaticApprovalFailed('到期草稿有收录失败，保留待审并稍后重试')
        await run_in_threadpool(tasks.finish,task,now=self.clock(),result=result)
