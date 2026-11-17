"""Find due drafts, then reuse normal review with source and task fencing."""

from psycopg.types.json import Jsonb

from .daily_cards import DailyCardStore
from .daily_tasks import require_task
from .db import connect
from .imports import direct_import_draft_filter
from .review import ReviewStore
from .review_policy import MANUAL_REVIEW
from .schema import DELAYED_APPROVAL_VERSION
from .space import RevisionConflict, read_policy


class ApprovalNotDue(ValueError):
    code='approval_not_due'


class PendingApprovalStore(ReviewStore):
    def __init__(self,dsn,space_id,policy):
        super().__init__(dsn,space_id)
        if self.schema_version<DELAYED_APPROVAL_VERSION:
            raise ValueError('到期自动入库需要先升级到版本 16')
        self.approval_policy=policy
        self.source_store=DailyCardStore(dsn,space_id)

    def due(self,ready_before):
        with connect(self.dsn) as c:
            if not read_policy(c,self.space_id)['auto_approve_enabled']:
                return []
            # Direct import owns approval for these drafts. Its failures are left
            # for a reviewer and must not be picked up by the daily sweep.
            return c.execute('''SELECT p.id FROM memory_core.pending_memories p WHERE p.bot_id=%s AND p.status='pending'
                AND p.review_ready_at::timestamptz<%s''' + direct_import_draft_filter(self.schema_version) + ' ORDER BY p.id',
                (self.space_id,ready_before)).fetchall()

    def plan(self,draft_id):
        with connect(self.dsn) as c:
            policy=read_policy(c,self.space_id)
            draft=self._draft(c,draft_id)
            if draft['status']!='pending':
                raise ApprovalNotDue('草稿已处理')
            extraction=self._extraction(c,draft['extraction_id'])
            # Daily extraction already stores the context/source snapshot. Do
            # not create a second copy or silently accept changed participants.
            origin=c.execute('''SELECT result FROM memory_core.daily_tasks WHERE bot_id=%s AND task_kind='extraction'
                AND result->'extraction_ids' @> %s ORDER BY business_date DESC LIMIT 1''',
                (self.space_id,Jsonb([draft['extraction_id']]))).fetchone()
            original=origin['result'] if origin else None
        if draft['policy_revision']!=policy['revision']:
            if not original:
                raise RevisionConflict('设置已改变，需人工复核草稿')
            self.reconfirm_extraction(extraction['id'],expected_version=extraction['version'],
                expected_policy_revision=policy['revision'],unchanged_context_digest=original['context_digest'])
            draft=self.get(draft_id)
        return dict(draft=draft,source=original)

    def guard(self,c,task,plan,*,now,due_at):
        policy=read_policy(c,self.space_id)
        draft=self._draft(c,plan['draft']['id'])
        if (not policy['auto_approve_enabled'] or draft['status']!='pending'
                or draft['review_ready_at']!=plan['draft']['review_ready_at']
                or not self.approval_policy.is_due(draft['review_ready_at'],due_at)):
            raise ApprovalNotDue('草稿尚未到期、已处理或自动入库已关闭')
        source=plan['source']
        if source:
            source_plan=dict(day=draft['business_date'],source_digest=source['source_digest'],source_turn_ids=source['source_turn_ids'])
            self.source_store.guard(c,task,source_plan,now=now)
        else:
            require_task(c,task)

    def require_manual(self,draft_id):
        # Version unchanged: a review page already open can still save and approve.
        with connect(self.dsn) as c:
            c.execute('''UPDATE memory_core.pending_memories SET error_code=%s
                WHERE bot_id=%s AND id=%s AND status='pending' ''',(MANUAL_REVIEW,self.space_id,draft_id))

    def still_due(self,draft_id,ready_before):
        with connect(self.dsn) as c:
            draft=self._draft(c,draft_id)
            return (read_policy(c,self.space_id)['auto_approve_enabled'] and draft['status']=='pending'
                and self.approval_policy.is_due(draft['review_ready_at'],ready_before))
