from datetime import date, datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch

from memory_core.config import Settings
from memory_core.daily_policy import DailySettings
from memory_core.db import connect
from memory_core.install import install
from memory_core.review import ReviewStore
from memory_core.review_policy import ApprovalSettings
from memory_core.space import Participant, SpaceConfiguration, RevisionConflict
from memory_core.testing.fixed_models import FixedModels
from memory_core.tests import test_review as fixtures
from memory_core.tests.support import DatabaseTestCase


class ApprovalPolicyTests(unittest.TestCase):
    def test_waiting_period_counts_business_days(self):
        core=Settings(3)
        policy=ApprovalSettings()
        # Drafts made just before or after the 03:15 run of 11-01 enter in the run for 11-03.
        self.assertEqual(policy.scheduled_at('2026-11-01T03:14:00+08:00',core,DailySettings()).isoformat(),'2026-11-03T03:15:00+08:00')
        self.assertEqual(policy.scheduled_at('2026-11-01T03:16:00+08:00',core,DailySettings()).isoformat(),'2026-11-03T03:15:00+08:00')
        self.assertEqual(policy.scheduled_at('2026-11-01T23:59:00+08:00',core,DailySettings()).isoformat(),'2026-11-03T03:15:00+08:00')
        before=policy.ready_before(date(2026,11,3),core)
        self.assertTrue(policy.is_due('2026-11-01T23:59:00+08:00',before))
        self.assertFalse(policy.is_due('2026-11-02T03:00:00+08:00',before))
        self.assertEqual(ApprovalSettings(72).delay_days,3)
        self.assertEqual(ApprovalSettings(30).delay_days,2)

    def test_custom_clock_and_daylight_saving_use_business_days(self):
        core=Settings(3,'America/New_York',4)
        ready='2026-10-31T04:30:00-04:00'
        policy=ApprovalSettings()
        self.assertEqual(policy.scheduled_at(ready,core,DailySettings(run_time='05:30')).isoformat(),'2026-11-02T05:30:00-05:00')
        self.assertEqual(policy.ready_before(date(2026,11,2),core).isoformat(),'2026-11-01T09:00:00+00:00')
        for value in (0,-1,True,'48'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                ApprovalSettings(value)
        with patch.dict(os.environ,{'MEMORY_AUTO_APPROVE_AFTER_HOURS':'72'}):
            self.assertEqual(ApprovalSettings.from_env().delay_hours,72)


class ReviewClockTests(DatabaseTestCase):
    ready=fixtures.ReviewTests.ready
    embeddings=fixtures.ReviewTests.embeddings
    approve=fixtures.ReviewTests.approve

    def setUp(self):
        super().setUp()
        install(self.owner,Settings(768))
        self.config=SpaceConfiguration(self.owner)
        self.config.configure('example',Participant('Example User'),Participant('Example Assistant'))
        self.store=ReviewStore(self.app,'example')
        self.topic=self.store.topics()[0]['id']
        self.vector=[1.0]+[0.0]*767

    def test_ready_clock_starts_on_completion_and_human_save_restarts_it(self):
        draft=self.ready()
        graph={k:v for k,v in draft['payload'].items() if k not in ('title','content','importance','evidence')}
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.pending_memories SET status='structure_failed',payload=NULL,review_ready_at=NULL WHERE id=%s",(draft['id'],))
        draft=self.store.get(draft['id'])
        model=FixedModels({'extract':{self.store.structure_prompt(draft['id']):json.dumps(graph)}})
        first=datetime.fromisoformat('2026-11-04T12:00:00+08:00')
        self.store._structure(draft,model,clock=lambda:first)
        current=self.store.get(draft['id'])
        self.assertEqual(datetime.fromisoformat(current['review_ready_at']),first)
        self.store._structure(current,model,clock=lambda:first+timedelta(hours=4))
        current=self.store.get(draft['id'])
        self.assertEqual(datetime.fromisoformat(current['review_ready_at']),first)
        saved=self.store.edit(current['id'],current['payload'],expected_version=current['version'],expected_policy_revision=current['policy_revision'],clock=lambda:first+timedelta(hours=8))
        self.assertEqual(datetime.fromisoformat(saved['review_ready_at']),first+timedelta(hours=8))
        with self.assertRaises(RevisionConflict):
            self.store.edit(current['id'],current['payload'],expected_version=current['version'],expected_policy_revision=current['policy_revision'])
        self.assertEqual(self.store.get(current['id'])['review_ready_at'],saved['review_ready_at'])

    def test_switch_preserves_wait_clock_and_never_bypasses_older_semantic_changes(self):
        draft=self.ready()
        old=draft['review_ready_at']
        updated=self.config.set_auto_approval('example',expected_revision=1,enabled=False)
        current=self.store.get(draft['id'])
        self.assertFalse(updated['auto_approve_enabled'])
        self.assertEqual(current['policy_revision'],2)
        self.assertEqual(current['review_ready_at'],old)
        with self.assertRaises(RevisionConflict):
            self.config.set_auto_approval('example',expected_revision=1,enabled=True)
        self.config.set_extraction_rules('example',expected_revision=2,minimum_importance=9)
        self.config.set_auto_approval('example',expected_revision=3,enabled=True)
        self.assertEqual(self.store.get(draft['id'])['policy_revision'],2)


class ApprovalMigrationTests(DatabaseTestCase):
    def test_upgrade_preserves_switch_choices_and_gives_existing_pending_a_fresh_clock(self):
        install(self.owner,Settings(3),target_version=15)
        config=SpaceConfiguration(self.owner)
        for space in ('automatic','manual'):
            config.configure(space,Participant('Synthetic User'),Participant('Synthetic Assistant'))
        config.set_review_mode('manual',expected_revision=1,review_mode='manual')
        with connect(self.owner) as c:
            material=c.execute("INSERT INTO memory_core.source_materials(bot_id,source_key,content_sha256,business_date,source_kind,segments) VALUES ('automatic','synthetic','hash','2026-11-01','mainline','[]') RETURNING id").fetchone()['id']
            extraction=c.execute("INSERT INTO memory_core.material_extractions(bot_id,material_id,request_key,policy_revision) VALUES ('automatic',%s,'synthetic',1) RETURNING id",(material,)).fetchone()['id']
            c.execute("INSERT INTO memory_core.pending_memories(bot_id,extraction_id,ordinal,business_date,policy_revision,narrative,payload,status,created_at) VALUES ('automatic',%s,0,'2026-11-01',1,'{}','{}','pending','2020-01-01T00:00:00+08:00')",(extraction,))
            tables=c.execute("SELECT tablename FROM pg_tables WHERE schemaname='memory_core' ORDER BY 1").fetchall()
        before=datetime.now(timezone.utc)-timedelta(seconds=1)
        install(self.owner,Settings(3),target_version=16)
        self.assertFalse(install(self.owner,Settings(3),target_version=16))
        with connect(self.app) as c:
            flags={r['bot_id']:r['auto_approve_enabled'] for r in c.execute('SELECT bot_id,auto_approve_enabled FROM memory_core.space_policies')}
            self.assertEqual(flags,{'automatic':True,'manual':False})
            ready=c.execute('SELECT review_ready_at FROM memory_core.pending_memories').fetchone()['review_ready_at']
            self.assertGreaterEqual(datetime.fromisoformat(ready),before)
            self.assertEqual(c.execute("SELECT tablename FROM pg_tables WHERE schemaname='memory_core' ORDER BY 1").fetchall(),tables)
