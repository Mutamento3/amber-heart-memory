"""Synthetic proposals for actual browser interaction, never runtime defaults."""

from datetime import datetime, timezone

from memory_core.daily.daily_tasks import DailyTasks
from memory_core.storage.db import connect
from memory_core.storage.postgres import PostgresStore
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.testing.fixed_models import FixedModels
from memory_core.lifecycle.topic_summaries import TopicSummaryStore
from memory_core.lifecycle.topic_summary_contract import retrieval_summary
from memory_core.lifecycle.topic_summary_policy import TopicSummarySettings


def seed(owner, app, fixtures):
    moment=datetime.now(timezone.utc)
    for suffix in ('mobile','desktop'):
        space='summary-'+suffix
        SpaceConfiguration(owner).configure(space,Participant('合成读者'),Participant('合成园丁'))
        store=TopicSummaryStore(app,space,TopicSummarySettings(2,30))
        for key in ('others','work','health','interests'):
            topic=next(t for t in store.topics() if t['preset_key']==key)
            for index in range(3):
                with connect(app) as c:
                    mid=PostgresStore(app,space).write_card('合成读者和合成园丁一起记录了植物观察与生活安排。这是专供验收的虚构材料。',[1,0,0],
                        title=topic['name']+'的合成片段'+str(index+1),source_date='2026-10-01',connection=c)
                    c.execute('UPDATE memory_core.memory_items SET primary_topic_id=%s WHERE id=%s',(topic['id'],mid))
                    c.execute('INSERT INTO memory_core.memory_topics(bot_id,memory_id,topic_id) VALUES (%s,%s,%s)',(space,mid,topic['id']))
        tasks=DailyTasks(app,space)
        task=tasks.claim('topic_summaries','2026-11-03',now=moment)
        for plan in store.plans('2026-11-03',now=moment):
            prepared=store.prepare(task,plan,now=moment)
            result=dict(topic_summaries=[dict(title=plan['topic']['name']+'：一起整理的十月',content='合成读者在十月记录了植物观察，并和合成园丁一起整理生活安排。事情还在继续，两人先留下当时的记录。')],
                children=[dict(id=row['id'],status='written' if index<2 else 'skipped') for index,row in enumerate(plan['cards'])])
            batch=store.publish(task,prepared['batch']['id'],result,now=moment)
            fixtures['embed'][retrieval_summary(batch['content'])]=[1,0,0]
            if plan['topic']['preset_key'] in ('others','work'):
                saved=store.approve(batch['id'],expected_version=batch['version'],expected_policy_revision=batch['policy_revision'],
                    request_key='synthetic-summary:'+str(batch['id']),embedder=FixedModels(fixtures),now=moment)
                if plan['topic']['preset_key']=='others':
                    with connect(app) as c:
                        c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=%s WHERE id=%s",(moment.isoformat(),saved['summary_card_id']))
        tasks.finish(task,now=moment)
