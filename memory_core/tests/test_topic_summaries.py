from concurrent.futures import ThreadPoolExecutor
from threading import Event
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
import re
from unittest.mock import patch

import psycopg

from memory_core.lifecycle.archives import ArchiveStore
from memory_core.lifecycle.archive_policy import ArchiveSettings
from memory_core.cards.cards import CardStore
from memory_core.storage.config import Settings
from memory_core.daily.daily_policy import DailySettings
from memory_core.daily.daily_tasks import DailyTasks, TaskLost
from memory_core.storage.db import connect
from memory_core.storage.install import install, MIGRATIONS
from memory_core.storage.parents import replace_parents
from memory_core.storage.postgres import PostgresStore
from memory_core.cards.review import PreparationError
from memory_core.cards.review_policy import ApprovalSettings
from memory_core.recall.search_store import SearchStore
from memory_core.storage.space import Participant, SpaceConfiguration, RevisionConflict
from memory_core.tests.support import DatabaseTestCase
from memory_core.lifecycle.topic_summaries import TopicSummaryStore, SummarySourceChanged
from memory_core.lifecycle.topic_summary_contract import validate_response, TopicSummaryFormatError
from memory_core.lifecycle.topic_summary_policy import TopicSummarySettings, plan_windows, next_month, event_date
from memory_core.lifecycle.topic_summary_text import build_prompt

NOW = datetime.fromisoformat('2026-11-03T03:16:00+08:00')

class Embeddings:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback
    def embed(self, text):
        self.calls.append(text)
        if self.callback:
            self.callback()
        return [1,0,0]


def response(cards, written=None):
    written = written if written is not None else [cards[0]['id']]
    return dict(topic_summaries=[dict(title='植物学习计划的变化',content='合成读者在十月学习植物，并和合成助手一起整理观察笔记。')] if written else [],
        children=[dict(id=c['id'],status='written' if c['id'] in written else 'skipped') for c in cards])


class WindowTests(unittest.TestCase):
    def cards(self, dates):
        return [dict(id=i+1,source_date=day,created_at='2026-11-01T00:00:00+08:00') for i,day in enumerate(dates)]
    def plans(self, dates, **kwargs):
        return plan_windows(self.cards(dates),cycle_month='2026-11',core=Settings(3),policy=TopicSummarySettings(2,4),**kwargs)
    def test_accumulate_closed_months_and_exclude_current_and_future(self):
        rows=self.plans(['2026-08-01','2026-09-03','2026-10-02','2026-11-01','2027-01-01'])
        self.assertEqual([(r['start_month'],r['end_month'],r['state']) for r in rows],[('2026-08','2026-09','ready'),('2026-10','2026-10','waiting')])
        self.assertEqual(next_month('2026-12'),'2027-01')
        self.assertEqual(event_date(self.cards([None])[0],Settings(3)).isoformat(),'2026-11-01')
    def test_year_boundary_timezone_and_reserved_tail(self):
        rows=plan_windows(self.cards(['2025-12-01','2026-01-01']),cycle_month='2026-02',core=Settings(3),policy=TopicSummarySettings(2,30))
        self.assertEqual((rows[0]['start_month'],rows[0]['end_month']),('2025-12','2026-01'))
        row=self.cards(['2026-10-01T00:30:00+00:00'])[0]
        self.assertEqual(event_date(row,Settings(3,timezone='America/Los_Angeles')).isoformat(),'2026-09-30')
        reserved=self.plans(['2026-09-01'],blocked=[('2026-10','2026-10')])[0]
        self.assertEqual(reserved['end_month'],'2026-09')
        with self.assertRaises(ValueError):event_date(self.cards(['invalid'])[0],Settings(3))
        partial=self.cards(['2025-07','2025'])
        self.assertEqual([event_date(row,Settings(3)).isoformat() for row in partial],['2025-07-01','2025-01-01'])
        self.assertEqual([(r['start_month'],r['end_month']) for r in self.plans(['2025','2025-07'])],[('2025-01','2025-07')])
        with self.assertRaises(ValueError):TopicSummarySettings(0,30)

    def test_whole_month_overflow_is_not_truncated(self):
        row=self.plans(['2026-10-01']*5)[0]
        self.assertEqual((row['state'],len(row['cards'])),('overflow',5))
    def test_rejected_window_extends_even_if_later_month_has_no_cards(self):
        dates=['2026-09-01','2026-09-02']
        row=self.plans(dates,deferred=[('2026-09','2026-09')])[0]
        self.assertEqual((row['end_month'],row['state']),('2026-10','ready'))
        self.assertEqual(self.plans(dates,blocked=[('2026-09','2026-09')]),[])
    def test_prompt_uses_space_names_health_style_and_possible_milestone(self):
        card=self.cards(['2026-10-01'])[0] | dict(title='合成健康记录',content='合成读者记录了症状。',is_milestone=True)
        text=build_prompt(dict(name='健康与身体',preset_key='health'),[card],dict(user=dict(name='合成读者'),assistant=dict(name='合成助手')),Settings(3),'2026-10','2026-10')
        for phrase in ('合成读者','合成助手','病历式','可能有误','不是必须写入','⭐'):
            self.assertIn(phrase,text)
        self.assertNotIn('skipped_reason',text)
    def test_contract_requires_exact_coverage_and_no_reason_classification(self):
        valid=response(self.cards(['2026-10-01','2026-10-02']))
        self.assertEqual(validate_response(valid,[1,2]),valid)
        for mutate in (lambda r:r['children'].pop(),lambda r:r['children'].append(r['children'][0]),
                       lambda r:r['children'][0].update(id=True),lambda r:r['children'][0].update(reason='small'),
                       lambda r:r['topic_summaries'][0].update(content='引用 [1]')):
            changed=deepcopy(valid); mutate(changed)
            with self.assertRaises(TopicSummaryFormatError): validate_response(changed,[1,2])
        self.assertEqual(validate_response(response(self.cards(['2026-10-01']),[]),[1])['topic_summaries'],[])

    def test_existing_summary_background_is_short_and_separate_from_numbered_sources(self):
        cards=self.cards(['2026-10-01'])
        cards[0].update(title='本月的新进展',content='合成读者继续观察植物。',is_milestone=False)
        args=(dict(name='兴趣爱好',preset_key='interests'),cards,
            dict(user=dict(name='合成读者'),assistant=dict(name='合成助手')),Settings(3),'2026-10','2026-10')
        text=build_prompt(*args,existing_summaries=[dict(title='旧的\n阶段总结',content='甲'*799+'\n乙'+'丙'*100)])
        self.assertIn('该主题已有的总结（仅作避免重复的背景，不是本窗口来源）',text)
        self.assertIn('- 旧的 阶段总结：'+'甲'*799+' ...',text)
        self.assertNotIn('丙',text)
        self.assertEqual(re.findall(r'^\[(\d+)\]',text,re.M),['1'])
        self.assertNotIn('该主题已有的总结',build_prompt(*args))


class TopicSummaryTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.core=Settings(3)
        install(self.owner,self.core)
        self.config=SpaceConfiguration(self.owner)
        for space in ('one','two'):
            self.config.configure(space,Participant('合成读者'),Participant('合成助手'))
        self.store=TopicSummaryStore(self.app,'one',TopicSummarySettings(2,30))
        self.tasks=DailyTasks(self.app,'one')
        self.embedder=Embeddings()
        self.topic=next(t['id'] for t in self.store.topics() if t['preset_key']=='interests')
    def card(self, day='2026-10-01', *, space='one'):
        with connect(self.app) as c:
            mid=PostgresStore(self.app,space).write_card('合成读者记录了植物学习的过程。',[1,0,0],title='植物观察',source_date=day,connection=c)
            topic=self.topic if space=='one' else c.execute("SELECT id FROM memory_core.topics WHERE bot_id='two' AND preset_key='interests'").fetchone()['id']
            c.execute('UPDATE memory_core.memory_items SET primary_topic_id=%s,created_at=%s WHERE id=%s',(topic,NOW.isoformat(),mid))
            c.execute('INSERT INTO memory_core.memory_topics(bot_id,memory_id,topic_id) VALUES (%s,%s,%s)',(space,mid,topic))
        return mid
    def ready(self, *, cycle='2026-11-03', day='2026-10-01', written=None):
        if not self.store.plans(cycle,now=NOW):
            self.card(day);self.card(day)
        task=self.tasks.claim('topic_summaries',cycle,now=NOW)
        plan=next(p for p in self.store.plans(cycle,now=NOW) if p['state']=='ready')
        prepared=self.store.prepare(task,plan,now=NOW)
        batch=self.store.publish(task,prepared['batch']['id'],response(plan['cards'],written),now=NOW)
        self.tasks.finish(task,now=NOW)
        return batch,plan
    def approve(self,batch,embedder=None,store=None):
        return (store or self.store).approve(batch['id'],expected_version=batch['version'],expected_policy_revision=batch['policy_revision'],
            request_key='approve:'+str(batch['id']),embedder=embedder or self.embedder,now=NOW)
    def rows(self):
        with connect(self.app) as c:
            return {r['id']:r for r in c.execute("SELECT * FROM memory_core.memory_items WHERE bot_id='one'")}
    def test_apply_only_written_and_search_children_show_summary(self):
        batch,plan=self.ready(); cards=plan['cards']
        eid=self.store.create_entity('合成植物',entity_type='OBJECT')
        with connect(self.app) as c:
            c.execute("INSERT INTO memory_core.memory_entities(bot_id,memory_id,entity_id,mention_role) VALUES ('one',%s,%s,'object')",(cards[0]['id'],eid))
        saved=self.approve(batch);parent=saved['summary_card_id'];rows=self.rows()
        self.assertEqual(rows[cards[0]['id']]['parent_reflection_id'],parent)
        self.assertEqual(rows[cards[1]['id']]['gardener_result'],'retained')
        self.assertIsNone(rows[cards[1]['id']]['parent_reflection_id'])
        self.assertEqual(rows[parent]['child_episode_ids'],[cards[0]['id']])
        self.assertEqual(self.approve(batch)['summary_card_id'],parent)
        self.assertEqual(len(self.embedder.calls),1)
        with connect(self.app) as c:
            self.assertTrue(c.execute('SELECT 1 FROM memory_core.memory_keyword_tokens WHERE memory_id=%s',(parent,)).fetchone())
            self.assertEqual(c.execute('SELECT entity_id FROM memory_core.memory_entities WHERE memory_id=%s',(parent,)).fetchone()['entity_id'],eid)
        candidates=[dict(memory_id=r['id'],final_score=1) for r in cards]
        result=replace_parents(SearchStore(self.app,'one'),candidates,{})
        self.assertEqual({r['memory_id'] for r in result},{parent,cards[1]['id']})
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=%s WHERE id=%s",(NOW.isoformat(),parent))
        result=replace_parents(SearchStore(self.app,'one'),candidates,{})
        self.assertEqual([r['memory_id'] for r in result],[cards[1]['id']])
        ArchiveStore(self.app,'one').restore(parent,expected_archived_at=NOW.isoformat(),now=NOW+timedelta(days=1))
        self.assertEqual({r['memory_id'] for r in replace_parents(SearchStore(self.app,'one'),candidates,{})},{parent,cards[1]['id']})

    def test_generation_background_uses_only_effective_summaries_in_same_space_and_topic(self):
        batch,_=self.ready();saved=self.approve(batch);parent=saved['summary_card_id']
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=%s WHERE id=%s",(NOW.isoformat(),parent))
            other_topic=c.execute("SELECT id FROM memory_core.topics WHERE bot_id='one' AND preset_key='work'").fetchone()['id']
            other_space_topic=c.execute("SELECT id FROM memory_core.topics WHERE bot_id='two' AND preset_key='interests'").fetchone()['id']
            for space,topic,title in [('one',other_topic,'别的主题不能带入'),('two',other_space_topic,'别的空间不能带入')]:
                mid=PostgresStore(self.app,space).write_card('供隔离验证的合成旧总结。',[1,0,0],title=title,item_kind='topic_summary',connection=c)
                c.execute('UPDATE memory_core.memory_items SET primary_topic_id=%s WHERE id=%s',(topic,mid))
        self.card('2026-11-01');self.card('2026-11-02')
        later=NOW.replace(month=12)
        task=self.tasks.claim('topic_summaries','2026-12-03',now=later)
        plan=next(p for p in self.store.plans('2026-12-03',now=later) if p['state']=='ready')
        prepared=self.store.prepare(task,plan,now=later)
        text=prepared['prompt']
        self.assertIn('该主题已有的总结',text)
        self.assertIn(batch['title'],text)
        self.assertIn('合成读者在十月学习植物',text)
        self.assertNotIn('别的主题不能带入',text);self.assertNotIn('别的空间不能带入',text)
        self.assertEqual([int(mid) for mid in re.findall(r'^\[(\d+)\]',text,re.M)],[r['id'] for r in plan['cards']])
        with self.assertRaises(TopicSummaryFormatError):
            validate_response(response(plan['cards'])|{'children':[dict(id=parent,status='written')]},[r['id'] for r in plan['cards']])
        self.store.revoke(batch['id'],expected_version=saved['version'],now=later,daily=DailySettings())
        retry=self.store.prepare(task,plan,now=later)
        self.assertEqual(retry['batch']['id'],prepared['batch']['id'])
        self.assertNotIn('该主题已有的总结',retry['prompt'])

    def test_pending_and_rejected_summaries_are_not_generation_background(self):
        pending,_=self.ready()
        self.card('2026-11-01');self.card('2026-11-02')
        later=NOW.replace(month=12)
        task=self.tasks.claim('topic_summaries','2026-12-03',now=later)
        plan=next(p for p in self.store.plans('2026-12-03',now=later) if p['state']=='ready')
        prepared=self.store.prepare(task,plan,now=later)
        self.assertNotIn(pending['title'],prepared['prompt'])
        self.assertNotIn('该主题已有的总结',prepared['prompt'])
        self.store.reject(pending['id'],expected_version=pending['version'],now=NOW,daily=DailySettings())
        retry=self.store.prepare(task,plan,now=later)
        self.assertNotIn(pending['title'],retry['prompt'])
        self.assertNotIn('该主题已有的总结',retry['prompt'])
    def test_summary_and_children_stay_protected_and_revoke_is_whole(self):
        batch,plan=self.ready();saved=self.approve(batch);parent=saved['summary_card_id']
        child=plan['cards'][0]['id']; skipped=plan['cards'][1]['id']
        with self.assertRaises(psycopg.errors.CheckViolation),connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=%s WHERE id=%s",(NOW.isoformat(),child))
        self.assertFalse(ArchiveSettings().due(self.rows()[parent],NOW+timedelta(days=180)))
        result=self.store.revoke(batch['id'],expected_version=saved['version'],now=NOW+timedelta(days=1),daily=DailySettings())
        rows=self.rows();self.assertNotIn(parent,rows)
        self.assertIsNone(rows[child]['parent_reflection_id']); self.assertIsNotNone(rows[child]['restored_at'])
        self.assertIsNone(rows[skipped]['gardener_result']);self.assertIsNone(rows[skipped]['restored_at'])
        self.assertEqual(self.store.revoke(batch['id'],expected_version=saved['version'],now=NOW,daily=DailySettings()),result)
        details=self.store.detail(batch['id']);self.assertEqual(details['batch']['content'],'')
        self.assertEqual(self.store.plans('2026-11-03',now=NOW+timedelta(days=1)),[])
        next_plan=self.store.plans('2026-12-03',now=NOW+timedelta(days=31))[0]
        self.assertEqual(next_plan['end_month'],'2026-11')
    def test_revoke_locks_children_before_parent_like_archival(self):
        batch,plan=self.ready();saved=self.approve(batch)
        reached=Event()
        class Observe(TopicSummaryStore):
            def _checkpoint(self,stage,c):
                if stage=='before_revoke_lock':reached.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with connect(self.app) as c:
                c.execute('SELECT id FROM memory_core.memory_items WHERE id=%s FOR UPDATE',(plan['cards'][0]['id'],))
                future=pool.submit(Observe(self.app,'one').revoke,batch['id'],expected_version=saved['version'],now=NOW,daily=DailySettings())
                self.assertTrue(reached.wait(3))
                c.execute('SELECT id FROM memory_core.memory_items WHERE id=%s FOR UPDATE NOWAIT',(saved['summary_card_id'],))
            self.assertEqual(future.result(timeout=5)['status'],'revoked')

    def test_deleting_absorbed_child_clears_hash_and_does_not_change_summary(self):
        batch,plan=self.ready();saved=self.approve(batch);parent=saved['summary_card_id'];before=self.rows()[parent]
        child=plan['cards'][0]['id'];CardStore(self.app,'one').delete(child)
        with connect(self.app) as c:
            link=c.execute('SELECT * FROM memory_core.topic_summary_sources WHERE batch_id=%s AND ordinal=0',(batch['id'],)).fetchone()
            self.assertIsNone(link['memory_id']);self.assertIsNone(link['source_hash'])
        self.assertEqual(self.rows()[parent],before)
        self.store.revoke(batch['id'],expected_version=saved['version'],now=NOW,daily=DailySettings())
        self.assertNotIn(child,self.rows())
    def test_metadata_edit_after_approval_does_not_mutate_summary(self):
        batch,plan=self.ready();saved=self.approve(batch);before=self.rows()[saved['summary_card_id']]
        CardStore(self.app,'one').edit(plan['cards'][0]['id'],dict(title='修改合成标题',topic_ids=[self.topic],is_milestone=True))
        self.assertEqual(self.rows()[saved['summary_card_id']],before)
    def test_source_edit_delete_or_archive_before_approval_refuses(self):
        batch,plan=self.ready();mid=plan['cards'][0]['id']
        CardStore(self.app,'one').edit(mid,dict(title='修改合成标题',topic_ids=[self.topic],is_milestone=True))
        with self.assertRaises(SummarySourceChanged):self.approve(batch)
        self.assertEqual(self.embedder.calls,[])
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET title='植物观察',is_milestone=false,archive_reason='decay',archived_at=%s WHERE id=%s",(NOW.isoformat(),mid))
        with self.assertRaises(SummarySourceChanged):self.approve(batch)
        CardStore(self.app,'one').delete(mid)
        with self.assertRaises(SummarySourceChanged):self.approve(batch)
        self.assertEqual(self.store.detail(batch['id'])['batch']['status'],'pending')
    def test_inflight_source_change_is_rechecked_after_embedding(self):
        batch,plan=self.ready();mid=plan['cards'][0]['id']
        def change():CardStore(self.app,'one').delete(mid)
        with self.assertRaises(SummarySourceChanged):self.approve(batch,Embeddings(change))
        self.assertFalse(any(r['item_kind']=='topic_summary' for r in self.rows().values()))
    def test_config_change_stale_version_cross_space_and_no_model_on_reject(self):
        batch,plan=self.ready()
        with self.assertRaises(ValueError):TopicSummaryStore(self.app,'two').detail(batch['id'])
        with self.assertRaises(RevisionConflict):self.approve(batch|{'version':batch['version']+1})
        result=self.store.reject(batch['id'],expected_version=batch['version'],now=NOW,daily=DailySettings())
        self.assertEqual(result['status'],'rejected');self.assertEqual(self.embedder.calls,[])
        self.assertEqual(self.store.plans('2026-11-03',now=NOW+timedelta(days=1)),[])
        rows=self.store.plans('2026-12-03',now=NOW+timedelta(days=31))
        self.assertEqual([(r['start_month'],r['end_month']) for r in rows],[('2026-10','2026-11')])
    def test_waiting_clock_and_shared_auto_switch_do_not_reset(self):
        batch,plan=self.ready();approval=ApprovalSettings();core=Settings(3)
        # Ready on business day 11-03, so due in the run for 11-05, not 11-04.
        self.assertEqual(self.store.due(approval.ready_before(date(2026,11,4),core),approval),[])
        self.assertEqual(len(self.store.due(approval.ready_before(date(2026,11,5),core),approval)),1)
        self.config.set_auto_approval('one',enabled=False,expected_revision=1)
        self.assertEqual(self.store.due(NOW+timedelta(days=3),approval),[])
        self.config.set_auto_approval('one',enabled=True,expected_revision=2)
        self.config.set_core_memory('one',expected_revision=3,core_memory='合成核心记忆')
        current=self.store.detail(batch['id'])['batch']
        self.assertEqual(current['review_ready_at'],batch['review_ready_at'])
        self.assertEqual((current['policy_revision'],current['version']),(4,batch['version']+3))
        self.assertEqual(len(self.store.due(NOW+timedelta(days=3),approval)),1)
    def test_publish_stale_or_sql_failure_and_apply_failure_roll_back(self):
        batch,plan=self.ready()
        class Fault(TopicSummaryStore):
            def _checkpoint(self,stage,c):
                if stage=='apply_sources':c.execute('SELECT 1/0')
        before=self.rows()
        with self.assertRaises(psycopg.errors.DivisionByZero):self.approve(batch,store=Fault(self.app,'one'))
        self.assertEqual(self.rows(),before);self.assertEqual(self.store.detail(batch['id'])['batch']['status'],'pending')
        with self.assertRaises(PreparationError):self.approve(batch,Embeddings(lambda:(_ for _ in ()).throw(ValueError('fake'))))
    def test_names_changed_before_approval_refuse_old_proposal(self):
        batch,plan=self.ready()
        self.config.configure('one',Participant('新合成读者'),Participant('合成助手'),expected_revision=1)
        current=self.store.detail(batch['id'])['batch']
        self.assertEqual(current['policy_revision'],2)
        with self.assertRaisesRegex(RevisionConflict,'参与者或主题设置已改变'):self.approve(current)
        self.assertEqual(self.embedder.calls,[])

    def test_empty_summary_keeps_all_sources_active_without_review(self):
        batch,plan=self.ready(written=[])
        self.assertEqual(batch['status'],'empty')
        self.assertTrue(all(r['gardener_result']=='retained' and r['parent_reflection_id'] is None for r in self.rows().values()))
        self.assertEqual(self.store.due(NOW+timedelta(days=3),ApprovalSettings()),[])
    def test_generation_source_deletion_or_publish_sql_error_leaves_no_half_proposal(self):
        self.card();self.card();task=self.tasks.claim('topic_summaries','2026-11-03',now=NOW)
        plan=self.store.plans('2026-11-03',now=NOW)[0];prepared=self.store.prepare(task,plan,now=NOW)
        class Fault(TopicSummaryStore):
            def _checkpoint(self,stage,c):
                if stage=='publish':c.execute('SELECT 1/0')
        with self.assertRaises(psycopg.errors.DivisionByZero):
            Fault(self.app,'one').publish(task,prepared['batch']['id'],response(plan['cards']),now=NOW)
        detail=self.store.detail(prepared['batch']['id'])
        self.assertEqual(detail['batch']['content'],'');self.assertEqual(detail['batch']['status'],'generating')
        self.assertTrue(all(s['disposition']=='unclassified' for s in detail['sources']))
        CardStore(self.app,'one').delete(plan['cards'][0]['id'])
        with self.assertRaises(SummarySourceChanged):self.store.publish(task,prepared['batch']['id'],response(plan['cards']),now=NOW)
        self.store.fail(task,prepared['batch']['id'],'topic_summary_source_changed',now=NOW)
        self.assertEqual(self.store.detail(prepared['batch']['id'])['batch']['status'],'failed')

    def test_generation_recovery_usage_and_task_fencing(self):
        self.card();self.card();task=self.tasks.claim('topic_summaries','2026-11-03',now=NOW)
        plan=self.store.plans('2026-11-03',now=NOW)[0];prepared=self.store.prepare(task,plan,now=NOW)
        event=dict(stage='topic_summary',model='synthetic-model',state='succeeded',usage=dict(total_tokens=12))
        self.store.record_usage(task,prepared['batch']['id'],event,now=NOW)
        newer=self.tasks.claim('topic_summaries','2026-11-03',now=NOW)
        with self.assertRaises(TaskLost):self.store.publish(task,prepared['batch']['id'],response(plan['cards']),now=NOW)
        self.store.recover(newer,now=NOW)
        self.assertEqual(self.store.detail(prepared['batch']['id'])['batch']['status'],'failed')
        retry=self.store.prepare(newer,plan,now=NOW)
        self.assertEqual(retry['batch']['id'],prepared['batch']['id'])
        self.assertEqual(len(retry['batch']['model_usage']),1)
        with connect(self.app) as c:
            self.assertEqual(len(c.execute("SELECT model_usage FROM memory_core.daily_tasks WHERE task_kind='topic_summaries'").fetchone()['model_usage']),1)


class TopicMigrationTests(DatabaseTestCase):
    def test_v17_upgrade_is_atomic_repeatable_and_adds_only_two_tables(self):
        core=Settings(3);install(self.owner,core,target_version=17)
        SpaceConfiguration(self.owner).configure('one',Participant('合成读者'),Participant('合成助手'))
        mid=PostgresStore(self.app,'one').write_card('合成升级材料',[1,0,0])
        with connect(self.app) as c:
            before=c.execute('SELECT * FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone()
        with tempfile.TemporaryDirectory() as folder:
            shutil.copytree(MIGRATIONS,Path(folder)/'migrations')
            path=Path(folder)/'migrations/018_topic_summaries.sql';path.write_text(path.read_text()+'\nSELECT 1/0;')
            with patch('memory_core.storage.install.MIGRATIONS',path.parent),self.assertRaises(psycopg.errors.DivisionByZero):install(self.owner,core)
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT version FROM memory_core.installation').fetchone()['version'],17)
            self.assertIsNone(c.execute("SELECT to_regclass('memory_core.topic_summary_batches') AS v").fetchone()['v'])
        self.assertTrue(install(self.owner,core));self.assertFalse(install(self.owner,core))
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT * FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone(),before)
            for table in ('topic_summary_batches','topic_summary_sources'):
                row=c.execute('SELECT has_table_privilege(current_user,%s,\'SELECT,INSERT,UPDATE\') AS allowed,has_table_privilege(current_user,%s,\'DELETE\') AS deletion',('memory_core.'+table,)*2).fetchone()
                self.assertTrue(row['allowed']);self.assertFalse(row['deletion'])
