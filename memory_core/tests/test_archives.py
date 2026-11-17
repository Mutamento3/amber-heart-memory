from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import psycopg
from psycopg.types.json import Jsonb

from memory_core.lifecycle.archive_policy import ArchiveSettings, receipt_health
from memory_core.lifecycle.archives import ArchiveStore
from memory_core.storage.clock import format_timestamp
from memory_core.storage.config import Settings
from memory_core.daily.daily_tasks import DailyTasks, TaskLost
from memory_core.storage.db import connect
from memory_core.storage.install import install, MIGRATIONS
from memory_core.storage.postgres import PostgresStore
from memory_core.storage.space import Participant, SpaceConfiguration, RevisionConflict
from memory_core.tests.support import DatabaseTestCase

NOW = datetime.fromisoformat('2026-11-04T03:16:00+08:00')


def seed_receipt(dsn, space='one', *, moment=NOW, status='reported_unused', injected=True,
                 history='current', state='complete', steps=1, position=None):
    """Synthetic rows use the actual installed conversation constraints."""
    with connect(dsn) as c:
        tid = c.execute("SELECT nextval(pg_get_serial_sequence('memory_core.conversation_turns','id')) AS id").fetchone()['id']
        stamp = moment.isoformat()
        c.execute('''INSERT INTO memory_core.conversation_turns
            (id,bot_id,position_id,generation_state,history_state,business_date,created_at,finished_at,recall_snapshot)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (tid, space, position or tid, state, history, moment.date().isoformat(), stamp,
             None if state in ('generating','waiting_tools') else stamp,
             Jsonb(dict(status='ok', cards=[dict(number=1,memory_id=1)] if injected else []))))
        for step in range(steps):
            c.execute('''INSERT INTO memory_core.conversation_requests
                (bot_id,turn_id,step_index,request_fingerprint,model,stream,state,started_at,usage_receipt)
                VALUES (%s,%s,%s,%s,'synthetic',false,'complete',%s,%s)''',
                (space, tid, step, '0'*64, stamp, Jsonb(dict(status=status))))
    return tid


class ArchivePolicyTests(unittest.TestCase):
    def row(self, **changes):
        return dict(item_kind='atomic', archive_reason=None, parent_reflection_id=None,
            gardener_result=None, decay_layer='tier_4_episode', is_milestone=False,
            last_used_at=None, restored_at=None, created_at=(NOW-timedelta(days=120)).isoformat(),
            source_date='1900-01-01') | changes

    def test_layer_boundaries_exemption_and_milestone_floor(self):
        settings = ArchiveSettings()
        for layer, days in [('tier_1_rule',365),('tier_2_reflection',180),('tier_3_semantic',180),
                            ('tier_4_episode',120),('tier_5_emotional',365)]:
            with self.subTest(layer=layer):
                row = self.row(decay_layer=layer, created_at=(NOW-timedelta(days=days)).isoformat())
                self.assertTrue(settings.due(row,NOW))
                self.assertFalse(settings.due(row,NOW-timedelta(seconds=1)))
        self.assertFalse(settings.due(self.row(decay_layer='tier_0_exempt',is_milestone=True),NOW+timedelta(days=1000)))
        self.assertFalse(settings.due(self.row(is_milestone=True),NOW+timedelta(days=244)))
        self.assertTrue(settings.due(self.row(is_milestone=True),NOW+timedelta(days=245)))
        self.assertEqual(ArchiveSettings(event_days=500).retention_days(self.row(is_milestone=True)),500)

    def test_current_presence_and_latest_usage_or_restoration_control_clock(self):
        settings=ArchiveSettings()
        for change in [dict(parent_reflection_id=1),
                       dict(gardener_result='absorbed'),dict(archive_reason='decay'),
                       dict(last_used_at=NOW.isoformat()),dict(restored_at=NOW.isoformat()),
                       dict(created_at=(NOW+timedelta(days=1)).isoformat())]:
            with self.subTest(change=change):
                self.assertFalse(settings.due(self.row(**change),NOW))
        row=self.row(created_at='2020-01-01T00:00:00+08:00',last_used_at=NOW.isoformat(),restored_at='2020-02-01T00:00:00+08:00')
        self.assertFalse(settings.due(row,NOW))
        # Fall-back day has 25 hours; use elapsed days rather than wall clocks.
        row=self.row(created_at='2026-10-31T04:30:00-04:00')
        settings=ArchiveSettings(event_days=2)
        self.assertTrue(settings.due(row,datetime.fromisoformat('2026-11-02T03:30:00-05:00')))
        self.assertFalse(settings.due(row,datetime.fromisoformat('2026-11-02T03:29:59-05:00')))

    def test_explicit_environment_configuration_is_validated(self):
        with patch.dict(os.environ,{'MEMORY_ARCHIVE_EVENT_DAYS':'125','MEMORY_ARCHIVE_RECEIPT_MIN_RATE':'0.9','MEMORY_ARCHIVE_RECEIPT_WINDOW_DAYS':'10'}):
            policy=ArchiveSettings.from_env()
            self.assertEqual((policy.event_days,policy.receipt_min_rate,policy.receipt_window_days),(125,0.9,10))
        for changes in [dict(event_days=0),dict(rule_days=True),dict(receipt_min_samples=-1),
                        dict(receipt_min_rate=0),dict(receipt_min_rate=1.1),dict(receipt_min_rate=float('nan'))]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                ArchiveSettings(**changes)
        with patch.dict(os.environ,{'MEMORY_ARCHIVE_EVENT_DAYS':'invalid'}),self.assertRaisesRegex(ValueError,'MEMORY_ARCHIVE_EVENT_DAYS'):
            ArchiveSettings.from_env()


class ArchiveTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.core=Settings(3)
        install(self.owner,self.core)
        config=SpaceConfiguration(self.owner)
        for space in ('one','two'):
            config.configure(space,Participant('Synthetic Reader'),Participant('Synthetic Assistant'))
        self.cards=PostgresStore(self.app,'one')
        self.policy=ArchiveSettings(receipt_min_samples=1)
        self.store=ArchiveStore(self.app,'one',self.policy)
        self.tasks=DailyTasks(self.app,'one')

    def card(self, **values):
        mid=self.cards.write_card('合成归档正文',[1,0,0],title='合成标题',summary='合成摘要',source_date='1900-01-01')
        data=dict(created_at=(NOW-timedelta(days=120)).isoformat(),decay_layer='tier_4_episode') | values
        with connect(self.app) as c:
            from psycopg import sql
            c.execute(sql.SQL('UPDATE memory_core.memory_items SET {} WHERE id=%s').format(
                sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in data)),[*data.values(),mid])
        return mid

    def row(self, mid):
        with connect(self.app) as c:
            return c.execute('SELECT * FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone()

    def run_archive(self, now=NOW):
        task=self.tasks.claim('archive',now.date().isoformat(),now=now)
        return self.store.archive_task(task,now=now)

    def test_receipt_gate_counts_current_finished_injected_rounds_once(self):
        for i in range(20):
            seed_receipt(self.app,status='reported' if i<8 else 'reported_unused' if i<16 else 'unreported',steps=3 if i==0 else 1)
        replaced=seed_receipt(self.app,history='replaced')
        for options in [dict(injected=False),dict(state='waiting_tools'),dict(space='two'),
                        dict(moment=NOW-timedelta(days=15)),dict(moment=NOW+timedelta(seconds=1))]:
            seed_receipt(self.app,**options)
        with connect(self.app) as c:
            health=receipt_health(c,'one',ArchiveSettings(),NOW)
        self.assertEqual((health['injected_rounds'],health['reported_rounds'],health['rate']),(20,16,0.8))
        self.assertFalse(health['paused'])
        seed_receipt(self.app,status='unreported',state='incomplete')
        with connect(self.app) as c:
            self.assertEqual(receipt_health(c,'one',ArchiveSettings(),NOW)['reason'],'low_receipt_rate')
            self.assertEqual(receipt_health(c,'two',ArchiveSettings(),NOW)['reason'],'insufficient_samples')

    def test_fewer_samples_or_unreported_rounds_never_archive(self):
        mid=self.card()
        result=self.run_archive()
        self.assertEqual(result['receipt_health']['reason'],'insufficient_samples')
        self.assertEqual(result['archived_count'],0)
        self.assertIsNone(self.row(mid)['archive_reason'])
        seed_receipt(self.app,status='unreported')
        result=self.run_archive(NOW+timedelta(days=1))
        self.assertEqual(result['receipt_health']['reason'],'low_receipt_rate')
        self.assertIsNone(self.row(mid)['archive_reason'])

    def test_archival_preserves_card_data_and_excludes_exempt_absorbed_summary_and_other_space(self):
        seed_receipt(self.app)
        due=self.card()
        before=self.row(due)
        summary=self.cards.write_card('合成主题总结',[0,1,0],item_kind='topic_summary')
        with connect(self.app) as c:
            topic = c.execute("SELECT id FROM memory_core.topics WHERE bot_id='one' AND preset_key='interests'").fetchone()['id']
            c.execute("UPDATE memory_core.memory_items SET created_at=%s WHERE id=%s",
                ((NOW-timedelta(days=500)).isoformat(), summary))
        pending = self.card(primary_topic_id=topic)
        retained = self.card(primary_topic_id=topic, gardener_result='retained')
        protected=[self.card(decay_layer='tier_0_exempt'),self.card(is_milestone=True),
            self.card(last_used_at=NOW.isoformat()),self.card(restored_at=NOW.isoformat()),
            self.card(parent_reflection_id=summary),self.card(gardener_result='absorbed'),
            self.card(created_at=NOW.isoformat()),pending]
        other=PostgresStore(self.app,'two').write_card('其他空间合成卡',[1,0,0])
        result=self.run_archive()
        self.assertEqual(result['archived_count'],2)
        self.assertEqual(self.row(retained)['archive_reason'],'decay')
        after=self.row(due)
        self.assertEqual(after.pop('archive_reason'),'decay')
        self.assertEqual(after.pop('archived_at'),format_timestamp(NOW,self.core))
        before.pop('archive_reason');before.pop('archived_at')
        self.assertEqual(after,before)
        for mid in protected+[summary,other]:
            self.assertIsNone(self.row(mid)['archive_reason'])
        self.assertIsNone(self.tasks.claim('archive',NOW.date().isoformat(),now=NOW))

    def test_restore_is_idempotent_preserves_usage_and_restarts_clock(self):
        seed_receipt(self.app)
        mid=self.card(used_count=7,last_used_at=(NOW-timedelta(days=120)).isoformat())
        self.run_archive()
        old=self.row(mid)
        self.assertNotIn(mid,[r["id"] for r in self.cards.search_vectors([1,0,0])])
        self.assertIn(mid,[r["id"] for r in self.cards.search_vectors([1,0,0],include_archived=True)])
        restored=NOW+timedelta(hours=1)
        result=self.store.restore(mid,expected_archived_at=old['archived_at'],now=restored)
        self.assertTrue(result['restored'])
        self.assertIn(mid,[r['id'] for r in self.cards.search_vectors([1,0,0])])
        row=self.row(mid)
        for key in ('content','summary','embedding','created_at','updated_at','last_used_at','used_count'):
            self.assertEqual(row[key],old[key])
        self.assertIsNone(row['archive_reason']);self.assertIsNone(row['archived_at'])
        self.assertFalse(self.policy.due(row,restored+timedelta(days=120)-timedelta(seconds=1)))
        self.assertTrue(self.policy.due(row,restored+timedelta(days=120)))
        repeated=self.store.restore(mid,expected_archived_at=old['archived_at'],now=restored+timedelta(days=1))
        self.assertFalse(repeated['restored'])
        self.assertEqual(self.row(mid)['restored_at'],result['restored_at'])
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=%s WHERE id=%s",((restored+timedelta(days=120)).isoformat(),mid))
        with self.assertRaises(RevisionConflict):
            self.store.restore(mid,expected_archived_at=old['archived_at'],now=restored+timedelta(days=121))
        with self.assertRaises(ValueError):
            ArchiveStore(self.app,'two').restore(mid,expected_archived_at=old['archived_at'],now=restored)

    def test_actual_model_receipt_updates_usage_clock_before_archival(self):
        from memory_core.conversations.conversations import ConversationStore
        used,unused=self.card(),self.card()
        store=ConversationStore(self.app,'one')
        body=dict(model='synthetic',messages=[dict(role='user',content='合成本轮消息')])
        record=store.begin(body,{'operation':'new'},now=NOW)
        store.save_context(record,body,store.history_input(record),dict(status='ok',cards=[
            dict(number=1,memory_id=used,content='合成用到的记忆'),
            dict(number=2,memory_id=unused,content='合成未用到的记忆')]))
        store.finish(record,content='合成回复',reported=[1],finish_reason='stop',now=NOW)
        self.assertEqual(self.run_archive()['archived_count'],1)
        self.assertIsNone(self.row(used)['archive_reason'])
        self.assertEqual(self.row(used)['used_count'],1)
        self.assertEqual(self.row(unused)['archive_reason'],'decay')

    def test_new_use_committed_before_lock_is_not_overwritten(self):
        seed_receipt(self.app)
        mid=self.card()
        def checkpoint(stage, connection):
            if stage=='before_lock':
                with connect(self.app) as other:
                    other.execute('UPDATE memory_core.memory_items SET last_used_at=%s WHERE id=%s',(NOW.isoformat(),mid))
        with patch.object(self.store,'_checkpoint',side_effect=checkpoint):
            self.assertEqual(self.run_archive()['archived_count'],0)
        self.assertIsNone(self.row(mid)['archive_reason'])

    def test_sql_failure_rolls_back_archival_restore_and_task_finish(self):
        seed_receipt(self.app)
        mid=self.card()
        task=self.tasks.claim('archive',NOW.date().isoformat(),now=NOW)
        def fault(stage, c):
            if stage in ('archive','restore'):
                c.execute('SELECT 1/0')
        with patch.object(self.store,'_checkpoint',side_effect=fault),self.assertRaises(psycopg.errors.DivisionByZero):
            self.store.archive_task(task,now=NOW)
        self.assertIsNone(self.row(mid)['archive_reason'])
        self.store.archive_task(task,now=NOW)
        archived=self.row(mid)
        with patch.object(self.store,'_checkpoint',side_effect=fault),self.assertRaises(psycopg.errors.DivisionByZero):
            self.store.restore(mid,expected_archived_at=archived['archived_at'],now=NOW+timedelta(hours=1))
        self.assertEqual(self.row(mid),archived)
        with self.assertRaises(TaskLost):
            self.store.archive_task(task,now=NOW)


class ArchiveMigrationTests(DatabaseTestCase):
    def test_v16_upgrade_adds_only_restoration_time_and_preserves_data_and_rollback(self):
        core=Settings(3)
        install(self.owner,core,target_version=16)
        SpaceConfiguration(self.owner).configure('one',Participant('Synthetic Reader'),Participant('Synthetic Assistant'))
        mid=PostgresStore(self.app,'one').write_card('合成升级材料',[1,0,0])
        with connect(self.app) as c:
            before=c.execute('SELECT * FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone()
            tables=c.execute("SELECT tablename FROM pg_tables WHERE schemaname='memory_core' ORDER BY 1").fetchall()
        with tempfile.TemporaryDirectory() as folder:
            shutil.copytree(MIGRATIONS,Path(folder)/'migrations')
            target=Path(folder)/'migrations/017_card_archiving.sql'
            target.write_text(target.read_text()+'\nSELECT 1/0;\n')
            with patch('memory_core.storage.install.MIGRATIONS',Path(folder)/'migrations'),self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner,core,target_version=17)
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT version FROM memory_core.installation').fetchone()['version'],16)
            self.assertEqual(c.execute('SELECT * FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone(),before)
        install(self.owner,core,target_version=17)
        self.assertFalse(install(self.owner,core,target_version=17))
        with connect(self.app) as c:
            after=c.execute('SELECT * FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone()
            self.assertIsNone(after.pop('restored_at'))
            self.assertEqual(after,before)
            self.assertEqual(c.execute("SELECT tablename FROM pg_tables WHERE schemaname='memory_core' ORDER BY 1").fetchall(),tables)
            column=c.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='memory_core' AND table_name='memory_items' AND column_name='restored_at'").fetchone()
            self.assertEqual(column['data_type'],'text')
