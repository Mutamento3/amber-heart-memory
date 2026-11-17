"""Durable monthly proposals; source bodies are read live, never copied here."""

from datetime import date, datetime

from psycopg.types.json import Jsonb

from .clock import format_timestamp
from .daily_tasks import DailyTasks, require_task
from .db import connect
from .extraction_contract import identifier
from .graph_store import GraphStore
from .schema import TOPIC_SUMMARY_VERSION
from .space import RevisionConflict, read_policy
from .topic_summary_contract import context_hash, digest, source_hash, validate_response
from .topic_summary_policy import TOPIC_KEYS, TopicSummarySettings, event_date, plan_windows
from .topic_summary_review import TopicSummaryReview
from .topic_summary_text import EVENT_SPAN, build_prompt


class SummarySourceChanged(RevisionConflict):
    code = 'topic_summary_source_changed'


class TopicSummaryStore(TopicSummaryReview, GraphStore):
    def __init__(self, dsn, space_id, settings=None):
        super().__init__(dsn, space_id)
        if self.schema_version < TOPIC_SUMMARY_VERSION:
            raise ValueError('主题总结需要先升级 memory_core 到版本 18')
        self.summary_policy = settings or TopicSummarySettings.from_env()
        self.tasks = DailyTasks(dsn,space_id)

    @staticmethod
    def _checkpoint(stage, connection):
        """Failure-injection seam; model calls never hold a transaction."""

    def _lock_review(self, c):
        policy = read_policy(c, self.space_id)
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('review:' + self.space_id,))
        return policy

    def _batch(self, c, batch_id, *, lock=False):
        identifier(batch_id, 'batch_id')
        row = c.execute('SELECT * FROM memory_core.topic_summary_batches WHERE bot_id=%s AND id=%s' + (' FOR UPDATE' if lock else ''),
            (self.space_id, batch_id)).fetchone()
        if row is None:
            raise ValueError('总结草稿不存在或属于其他空间')
        return row

    def _topic(self, c, topic_id):
        topic = c.execute('SELECT * FROM memory_core.topics WHERE bot_id=%s AND id=%s', (self.space_id, topic_id)).fetchone()
        if topic is None or topic['preset_key'] not in TOPIC_KEYS:
            raise ValueError('这个主题不在主题总结范围内')
        return topic

    def _sources(self, c, batch, *, validate=False, lock=False):
        links = c.execute('SELECT * FROM memory_core.topic_summary_sources WHERE bot_id=%s AND batch_id=%s ORDER BY ordinal',
            (self.space_id, batch['id'])).fetchall()
        ids = [r['memory_id'] for r in links if r['memory_id'] is not None]
        cards = c.execute('SELECT * FROM memory_core.memory_items WHERE bot_id=%s AND id=ANY(%s) ORDER BY id' + (' FOR UPDATE' if lock else ''),
            (self.space_id, ids)).fetchall() if ids else []
        by_id = {r['id']: r for r in cards}
        if validate:
            for link in links:
                card = by_id.get(link['memory_id'])
                if (card is None or card['item_kind'] != 'atomic' or card['archive_reason'] is not None
                        or card['parent_reflection_id'] is not None or card['gardener_result'] is not None
                        or card['primary_topic_id'] != batch['topic_id'] or source_hash(card) != link['source_hash']):
                    raise SummarySourceChanged('来源卡片已修改、归档、删除或被其他总结处理，请驳回后重新整理')
        return links, by_id

    def _check_context(self, c, batch, policy):
        topic = self._topic(c, batch['topic_id'])
        if context_hash(policy, topic) != batch['context_digest']:
            raise RevisionConflict('参与者或主题设置已改变，请驳回后重新整理')
        return topic

    def recover(self, task, *, now):
        with connect(self.dsn) as c:
            self._lock_review(c)
            require_task(c, task)
            c.execute("""UPDATE memory_core.topic_summary_batches SET status='failed',error_code='generation_interrupted',
                version=version+1,updated_at=%s WHERE bot_id=%s AND cycle_date=%s AND status='generating'""",
                (format_timestamp(now,self.settings), self.space_id, task['business_date']))

    def plans(self, cycle_date, *, now):
        cycle = date.fromisoformat(cycle_date)
        if cycle.day != 3:
            raise ValueError('主题总结周期必须是每月 3 日')
        with connect(self.dsn) as c:
            policy = read_policy(c, self.space_id)
            topics = c.execute('SELECT * FROM memory_core.topics WHERE bot_id=%s AND preset_key=ANY(%s) ORDER BY id',
                (self.space_id, list(TOPIC_KEYS))).fetchall()
            batches = c.execute("SELECT * FROM memory_core.topic_summary_batches WHERE bot_id=%s AND status IN ('generating','pending','rejected','revoked')", (self.space_id,)).fetchall()
            cards = c.execute("""SELECT * FROM memory_core.memory_items WHERE bot_id=%s AND item_kind='atomic'
                AND archive_reason IS NULL AND parent_reflection_id IS NULL AND gardener_result IS NULL ORDER BY id""", (self.space_id,)).fetchall()
        result = []
        for topic in topics:
            blocked, deferred = [], []
            for batch in batches:
                if batch['topic_id'] != topic['id']:
                    continue
                pair = (batch['window_start'], batch['window_end'])
                if batch['status'] in ('generating','pending') or (batch['reconsider_at'] and now < datetime.fromisoformat(batch['reconsider_at'])):
                    blocked.append(pair)
                else:
                    deferred.append(pair)
            windows = plan_windows([r for r in cards if r['primary_topic_id'] == topic['id']],
                cycle_month=cycle.strftime('%Y-%m'), core=self.settings, policy=self.summary_policy,
                blocked=blocked, deferred=deferred)
            for window in windows:
                result.append(dict(window, topic=topic, policy_revision=policy['revision'],
                    context_digest=context_hash(policy,topic), cycle_date=cycle_date))
        return result

    def prepare(self, task, plan, *, now):
        with connect(self.dsn) as c:
            policy = self._lock_review(c)
            require_task(c, task)
            topic = self._topic(c, plan['topic']['id'])
            signature = context_hash(policy, topic)
            if signature != plan['context_digest']:
                raise SummarySourceChanged('生成前空间设置已改变')
            ids = [r['id'] for r in plan['cards']]
            live = c.execute('SELECT * FROM memory_core.memory_items WHERE bot_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE',
                (self.space_id,ids)).fetchall()
            original = {r['id']: source_hash(r) for r in plan['cards']}
            if len(live) != len(ids) or any(source_hash(r) != original[r['id']] or r['archive_reason'] is not None
                    or r['parent_reflection_id'] is not None or r['gardener_result'] is not None for r in live):
                raise SummarySourceChanged('生成前来源卡片已改变')
            key = digest([task['business_date'],topic['id'],plan['start_month'],plan['end_month'],signature,sorted(original.items())])
            batch = c.execute('SELECT * FROM memory_core.topic_summary_batches WHERE bot_id=%s AND request_key=%s FOR UPDATE', (self.space_id,key)).fetchone()
            stamp = format_timestamp(now,self.settings)
            if batch and batch['status'] not in ('failed','generating'):
                return dict(batch=batch, prompt=None)
            if batch is None:
                batch = c.execute('''INSERT INTO memory_core.topic_summary_batches
                    (bot_id,topic_id,cycle_date,window_start,window_end,request_key,policy_revision,context_digest,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                    (self.space_id,topic['id'],task['business_date'],plan['start_month'],plan['end_month'],key,policy['revision'],signature,stamp,stamp)).fetchone()
                for ordinal, card in enumerate(plan['cards']):
                    c.execute('''INSERT INTO memory_core.topic_summary_sources(batch_id,bot_id,ordinal,memory_id,source_hash)
                        VALUES (%s,%s,%s,%s,%s)''',(batch['id'],self.space_id,ordinal,card['id'],original[card['id']]))
            else:
                batch = c.execute("""UPDATE memory_core.topic_summary_batches SET status='generating',error_code=NULL,
                    version=version+1,updated_at=%s WHERE id=%s AND bot_id=%s RETURNING *""", (stamp,batch['id'],self.space_id)).fetchone()
            # Formal summary cards already passed review; revoked ones are
            # deleted. Archived summaries still help avoid retelling old events.
            previous = c.execute('''SELECT title,content FROM memory_core.memory_items
                WHERE bot_id=%s AND primary_topic_id=%s AND item_kind='topic_summary'
                ORDER BY id''', (self.space_id,topic['id'])).fetchall()
            prompt = build_prompt(topic, plan['cards'], policy['participants'], self.settings,
                plan['start_month'], plan['end_month'], existing_summaries=previous)
            return dict(batch=batch, prompt=prompt)

    def record_usage(self, task, batch_id, event, *, now):
        with connect(self.dsn) as c:
            canonical = self.tasks.record_usage(task,event,now=now,connection=c)
            c.execute('''UPDATE memory_core.topic_summary_batches SET model_usage=model_usage || %s::jsonb,model=%s
                WHERE bot_id=%s AND id=%s''', (Jsonb([canonical]),event['model'],self.space_id,batch_id))

    def publish(self, task, batch_id, response, *, now):
        with connect(self.dsn) as c:
            policy = self._lock_review(c)
            require_task(c,task)
            batch = self._batch(c,batch_id,lock=True)
            if batch['status'] != 'generating':
                raise RevisionConflict('总结生成状态已改变')
            self._check_context(c,batch,policy)
            links, cards = self._sources(c,batch,validate=True,lock=True)
            result = validate_response(response,[r['memory_id'] for r in links])
            for child in result['children']:
                c.execute('UPDATE memory_core.topic_summary_sources SET disposition=%s WHERE bot_id=%s AND batch_id=%s AND memory_id=%s',
                    (child['status'],self.space_id,batch_id,child['id']))
            title, content, state = '', '', 'empty'
            if result['topic_summaries']:
                summary = result['topic_summaries'][0]
                dates = sorted(event_date(cards[r['id']],self.settings).isoformat() for r in result['children'] if r['status']=='written')
                title = summary['title']
                content = EVENT_SPAN.format(start=dates[0],end='' if dates[0]==dates[-1] else ' 至 '+dates[-1],content=summary['content'])
                state = 'pending'
            else:
                c.execute("UPDATE memory_core.memory_items SET gardener_result='retained' WHERE bot_id=%s AND id=ANY(%s)",
                    (self.space_id,list(cards)))
            stamp = format_timestamp(now,self.settings)
            self._checkpoint('publish',c)
            return c.execute('''UPDATE memory_core.topic_summary_batches SET title=%s,content=%s,status=%s,
                review_ready_at=%s,policy_revision=%s,version=version+1,error_code=NULL,updated_at=%s
                WHERE bot_id=%s AND id=%s RETURNING *''',
                (title,content,state,stamp if state=='pending' else None,policy['revision'],stamp,self.space_id,batch_id)).fetchone()

    def fail(self, task, batch_id, code, *, now):
        with connect(self.dsn) as c:
            require_task(c,task)
            c.execute("""UPDATE memory_core.topic_summary_batches SET status='failed',error_code=%s,version=version+1,updated_at=%s
                WHERE bot_id=%s AND id=%s AND status='generating'""", (code,format_timestamp(now,self.settings),self.space_id,batch_id))

    def detail(self, batch_id):
        with connect(self.dsn) as c:
            policy = read_policy(c,self.space_id)
            batch = self._batch(c,batch_id)
            topic = self._topic(c,batch['topic_id'])
            links, cards = self._sources(c,batch)
        sources = [dict(memory_id=r['memory_id'], disposition=r['disposition'],
            title=cards[r['memory_id']]['title'] if r['memory_id'] in cards else None,
            content=cards[r['memory_id']]['content'] if r['memory_id'] in cards else None) for r in links]
        return dict(batch=batch, topic=topic, sources=sources, policy_revision=policy['revision'])
