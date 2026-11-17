"""Read-only proposals; applying and undoing their effects is transactional."""

from psycopg.types.json import Jsonb

from ..cards.cards import cleanup_orphans
from ..storage.clock import format_timestamp
from ..storage.db import connect
from ..cards.extraction_contract import check_credentials, identifier, text
from ..cards.keyword_index import build_rows
from ..models.models import embed_for
from ..cards.pending_approval import ApprovalNotDue
from ..cards.review import PreparationError
from ..storage.space import RevisionConflict, read_policy
from .topic_summary_contract import retrieval_summary
from .topic_summary_policy import event_date, next_run_after


class TopicSummaryReview:
    @staticmethod
    def _result(batch):
        return {key: batch[key] for key in ('id','status','version','summary_card_id','review_key')}

    def approve(self, batch_id, *, expected_version, expected_policy_revision, request_key, embedder, now, guard=None):
        identifier(expected_version,'expected_version')
        identifier(expected_policy_revision,'expected_policy_revision')
        request_key = text(request_key,'review_request_key')
        check_credentials(request_key)
        with connect(self.dsn) as c:
            policy = self._lock_review(c)
            batch = self._batch(c,batch_id)
            if batch['status']=='approved':
                return self._result(batch)
            if guard:
                guard(c)
            if batch['status']!='pending' or batch['version']!=expected_version or policy['revision']!=expected_policy_revision:
                raise RevisionConflict('总结或空间设置已改变，请重新读取后确认')
            self._check_context(c,batch,policy)
            links, cards = self._sources(c,batch,validate=True)
            collision = c.execute('SELECT id FROM memory_core.topic_summary_batches WHERE bot_id=%s AND review_key=%s',
                (self.space_id,request_key)).fetchone()
            if collision and collision['id']!=batch_id:
                raise ValueError('审核请求已用于其他总结')
            content, title = batch['content'], batch['title']
        summary = retrieval_summary(content)
        try:
            if embedder is None:
                raise ValueError()
            vector = self._vector(embed_for(embedder, summary, purpose="document", title=title))
        except Exception:
            raise PreparationError('主题总结坐标计算失败，原卡片未改动') from None
        stamp = format_timestamp(now,self.settings)
        with connect(self.dsn) as c:
            policy = self._lock_review(c)
            batch = self._batch(c,batch_id,lock=True)
            if batch['status']=='approved':
                return self._result(batch)
            if guard:
                guard(c)
            if batch['status']!='pending' or batch['version']!=expected_version or policy['revision']!=expected_policy_revision:
                raise RevisionConflict('收录期间总结或设置已改变')
            topic = self._check_context(c,batch,policy)
            links, cards = self._sources(c,batch,validate=True,lock=True)
            written = [r['memory_id'] for r in links if r['disposition']=='written']
            skipped = [r['memory_id'] for r in links if r['disposition']=='skipped']
            if not written:
                raise ValueError('总结没有实际写入的来源')
            source_date = max(event_date(cards[mid],self.settings) for mid in written).isoformat()
            card = c.execute('''INSERT INTO memory_core.memory_items
                (bot_id,title,content,summary,embedding,item_kind,source_kind,decay_layer,source_date,
                 primary_topic_id,child_episode_ids,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s::public.vector,'topic_summary','gardener','tier_2_reflection',%s,%s,%s,%s,%s) RETURNING *''',
                (self.space_id,title,content,summary,vector,source_date,topic['id'],Jsonb(written),stamp,stamp)).fetchone()
            self._checkpoint('summary_card',c)
            c.execute('INSERT INTO memory_core.memory_topics(bot_id,memory_id,topic_id) VALUES (%s,%s,%s)',
                (self.space_id,card['id'],topic['id']))
            entities = c.execute('''SELECT e.* FROM memory_core.entities e WHERE e.bot_id=%s AND EXISTS(
                SELECT 1 FROM memory_core.memory_entities me WHERE me.bot_id=e.bot_id AND me.entity_id=e.id AND me.memory_id=ANY(%s))
                ORDER BY e.id''', (self.space_id,written)).fetchall()
            for entity in entities:
                c.execute("INSERT INTO memory_core.memory_entities(bot_id,memory_id,entity_id,mention_role) VALUES (%s,%s,%s,'background')",
                    (self.space_id,card['id'],entity['id']))
            rows = build_rows(card,entities,[topic],timestamp=stamp,ignored_terms=policy['ignored_terms'])
            c.execute('SELECT memory_core.replace_memory_keyword_tokens(%s,%s,%s)',(card['id'],self.space_id,Jsonb(rows)))
            c.execute("UPDATE memory_core.memory_items SET parent_reflection_id=%s,gardener_result='absorbed' WHERE bot_id=%s AND id=ANY(%s)",
                (card['id'],self.space_id,written))
            c.execute("UPDATE memory_core.memory_items SET gardener_result='retained' WHERE bot_id=%s AND id=ANY(%s)",(self.space_id,skipped))
            self._checkpoint('apply_sources',c)
            batch = c.execute("""UPDATE memory_core.topic_summary_batches SET status='approved',summary_card_id=%s,
                review_key=%s,reviewed_at=%s,policy_revision=%s,version=version+1,updated_at=%s
                WHERE bot_id=%s AND id=%s RETURNING *""",(card['id'],request_key,stamp,policy['revision'],stamp,self.space_id,batch_id)).fetchone()
            return self._result(batch)

    def reject(self,batch_id,*,expected_version,now,daily):
        identifier(expected_version,'expected_version')
        with connect(self.dsn) as c:
            self._lock_review(c)
            batch = self._batch(c,batch_id,lock=True)
            if batch['status']=='rejected':
                return self._result(batch)
            if batch['status']!='pending' or batch['version']!=expected_version:
                raise RevisionConflict('总结状态已改变，请刷新后再驳回')
            stamp = format_timestamp(now,self.settings)
            reconsider = format_timestamp(next_run_after(now,self.settings,daily),self.settings)
            batch = c.execute("""UPDATE memory_core.topic_summary_batches SET status='rejected',reviewed_at=%s,
                reconsider_at=%s,version=version+1,updated_at=%s WHERE bot_id=%s AND id=%s RETURNING *""",
                (stamp,reconsider,stamp,self.space_id,batch_id)).fetchone()
            return self._result(batch)

    def revoke(self,batch_id,*,expected_version,now,daily):
        identifier(expected_version,'expected_version')
        stamp = format_timestamp(now,self.settings)
        with connect(self.dsn) as c:
            policy = self._lock_review(c)
            batch = self._batch(c,batch_id,lock=True)
            if batch['status']=='revoked':
                return self._result(batch)
            if batch['status']!='approved' or batch['version']!=expected_version:
                raise RevisionConflict('总结状态已改变，请刷新后再撤销')
            parent = batch['summary_card_id']
            links, cards = self._sources(c,batch)
            self._checkpoint('before_revoke_lock',c)
            # Archival locks cards in ascending ID order too. Lock parent and
            # children together to avoid a parent/child lock-order deadlock.
            c.execute('SELECT id FROM memory_core.memory_items WHERE bot_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE',
                (self.space_id,[parent]+list(cards)))
            links, cards = self._sources(c,batch)
            written = [r['memory_id'] for r in links if r['disposition']=='written' and r['memory_id'] in cards]
            if any(cards[mid]['parent_reflection_id']!=parent for mid in written):
                raise RevisionConflict('来源已归到其他总结，不能覆盖当前状态')
            skipped = [r['memory_id'] for r in links if r['disposition']=='skipped' and r['memory_id'] in cards
                       and cards[r['memory_id']]['parent_reflection_id'] is None and cards[r['memory_id']]['gardener_result']=='retained']
            c.execute('''UPDATE memory_core.memory_items SET parent_reflection_id=NULL,gardener_result=NULL,restored_at=%s
                WHERE bot_id=%s AND id=ANY(%s)''',(stamp,self.space_id,written))
            c.execute('UPDATE memory_core.memory_items SET gardener_result=NULL WHERE bot_id=%s AND id=ANY(%s)',(self.space_id,skipped))
            candidates = [r['entity_id'] for r in c.execute('SELECT entity_id FROM memory_core.memory_entities WHERE bot_id=%s AND memory_id=%s',(self.space_id,parent))]
            reconsider = format_timestamp(next_run_after(now,self.settings,daily),self.settings)
            batch = c.execute("""UPDATE memory_core.topic_summary_batches SET status='revoked',summary_card_id=NULL,
                title='',content='',reconsider_at=%s,version=version+1,updated_at=%s WHERE bot_id=%s AND id=%s RETURNING *""",
                (reconsider,stamp,self.space_id,batch_id)).fetchone()
            c.execute('DELETE FROM memory_core.relations WHERE bot_id=%s AND source_memory_id=%s',(self.space_id,parent))
            c.execute('DELETE FROM memory_core.memory_items WHERE bot_id=%s AND id=%s',(self.space_id,parent))
            cleanup_orphans(c,self.space_id,candidates,[p['id'] for p in policy['participants'].values()])
            self._checkpoint('revoke',c)
            return self._result(batch)

    def due(self,cutoff,approval_policy):
        with connect(self.dsn) as c:
            policy = read_policy(c,self.space_id)
            if not policy['auto_approve_enabled']:
                return []
            return c.execute('''SELECT id,version,%s::bigint AS policy_revision FROM memory_core.topic_summary_batches
                WHERE bot_id=%s AND status='pending' AND review_ready_at::timestamptz<%s ORDER BY id''',
                (policy['revision'],self.space_id,cutoff)).fetchall()

    def guard_auto(self,c,task,batch_id,*,cutoff,approval_policy):
        from ..daily.daily_tasks import require_task
        require_task(c,task)
        batch = self._batch(c,batch_id)
        if (not read_policy(c,self.space_id)['auto_approve_enabled'] or batch['status']!='pending'
                or not approval_policy.is_due(batch['review_ready_at'],cutoff)):
            raise ApprovalNotDue('总结尚未到期或已在别处处理')

    def still_due(self,batch_id,cutoff,approval_policy):
        with connect(self.dsn) as c:
            batch = self._batch(c,batch_id)
            return (read_policy(c,self.space_id)['auto_approve_enabled'] and batch['status']=='pending'
                and approval_policy.is_due(batch['review_ready_at'],cutoff))
