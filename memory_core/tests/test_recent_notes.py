from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import psycopg

from memory_core.bookmarks.bookmarks import BookmarkStore
from memory_core.storage.config import Settings
from memory_core.daily.daily_tasks import DailyTasks, TaskLost
from memory_core.storage.db import connect
from memory_core.storage.install import install, MIGRATIONS
from memory_core.storage.postgres import PostgresStore
from memory_core.bookmarks.recent_notes import RecentNotesStore
from memory_core.bookmarks.recent_note_text import SECTIONS, char_count, render_recent_notes
from memory_core.bookmarks.recent_note_tidy import TidyPolicy, build_entries, prepare_request, parse_result, validate_result
from memory_core.storage.space import Participant, SpaceConfiguration, RevisionConflict
from memory_core.tests.support import DatabaseTestCase

NOW = datetime.fromisoformat('2026-11-09T03:20:00+08:00')


class TidyContractTests(unittest.TestCase):
    def test_counts_and_prompt_preserve_json_and_all_four_prefixes(self):
        self.assertEqual(char_count(' 甲\t乙\n\u3000\ufeff'), 2)
        sections = dict.fromkeys(SECTIONS, '2026年11月9日 事实')
        people = dict(user=dict(name='小禾'), assistant=dict(name='林舟'))
        prompt, material, entries = prepare_request(sections, TidyPolicy(), people, NOW.date())
        self.assertEqual(set(entries), {'周期1','健康1','长期1','日常1'})
        self.assertIn('"suggestions":[{"no":1', prompt)
        self.assertNotIn('{{', prompt)
        self.assertIn('现在没有超过字数线：只做合并和精简（规则一、二、三），不要提删除建议，规则四不适用。', prompt)
        self.assertIn('林舟', prompt)
        prompt, _, _ = prepare_request(sections, TidyPolicy(20,10), people, NOW.date())
        self.assertIn('现在超过字数线，要提删减建议，目标是让记忆书签降到10字左右，低一点也可以。', prompt)
        self.assertTrue(all('【'+entry+'】' in material for entry in entries))

    def test_validator_drops_invalid_overlapping_cross_section_and_star_deletions(self):
        entries = build_entries(dict(period='经期', health='★用药\n复查', longterm='项目', daily='日常'))
        candidates = [None, dict(type='未知',targets=['健康2'],new_text='新'),
            dict(type='合并',targets=['健康2','长期1'],new_text='新'),
            dict(type='删除',targets=['健康1'],new_text=''),
            dict(type='精简',targets=['不存在'],new_text='新'),
            dict(type='精简',targets=['健康2'],new_text='【健康关注】新'),
            dict(type='压缩',targets=['健康1'],new_text='药名\n剂量'),
            dict(type='压缩',targets=['健康1'],new_text='重复'),
            dict(type='精简',targets=['健康2','健康2'],new_text='重复')]
        result = validate_result(dict(suggestions=candidates,need_confirm=[dict(target='日常1',question='还重要吗'),dict(target='空',question='问题')]), entries,allow_delete=True)
        self.assertEqual(len(result['suggestions']),1)
        self.assertEqual(result['suggestions'][0]['new_text'],'★药名 剂量')
        self.assertEqual(len(result['need_confirm']),1)
        self.assertEqual(len(result['dropped']),9)
        result = validate_result(dict(suggestions=[dict(type='删除',targets=['日常1'],new_text='')]),entries,allow_delete=False)
        self.assertEqual(result['suggestions'],[])

    def test_parse_invalid_and_render_empty_sections(self):
        for content in ('no json','{}','{"suggestions":0}','{"suggestions":[],"need_confirm":0}'):
            with self.assertRaises(ValueError):
                parse_result(content)
        self.assertEqual(parse_result('```json\n{"suggestions":[]}\n```'),{'suggestions':[]})
        self.assertEqual(render_recent_notes(dict.fromkeys(SECTIONS,''),'小禾'),'')
        text = render_recent_notes(dict(health='事实',daily=''),'小禾')
        self.assertIn('<bookmarks>',text)
        self.assertIn('【健康关注】\n事实',text)
        self.assertNotIn('【日常杂记】',text)
        self.assertTrue(text.endswith('</bookmarks>'))


class RecentNotesTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner,Settings(3))
        for space in ('one','two'):
            SpaceConfiguration(self.owner).configure(space,Participant('用户'),Participant('助手'))
        self.notes = RecentNotesStore(self.app,'one')
        self.bookmarks = BookmarkStore(self.app,'one')
        self.policy = TidyPolicy()

    def tearDown(self):
        pass

    def add(self,content='事实',when='2026-11-08T12:00:00+08:00',section='daily'):
        return self.bookmarks.execute(dict(section=section,content=content),now=datetime.fromisoformat(when))

    def task(self):
        return DailyTasks(self.app,'one').claim('recent_notes','2026-11-09',now=NOW)

    def test_pending_crud_short_id_ambiguity_isolation_and_actual_deletes(self):
        identifiers = ['12345678-0000-4000-8000-000000000001','12345678-0000-4000-8000-000000000002']
        with patch('memory_core.bookmarks.bookmarks.uuid4',side_effect=identifiers):
            first = self.add('日期\r\n第一行\n第二行')
            self.add('另一件事')
        self.assertNotIn('\n',first['content'])
        result = self.bookmarks.execute(dict(action='update',ids=['12345678'],content='不能误改'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['ambiguous'],['12345678'])
        moved = self.bookmarks.execute(dict(action='update',ids=[identifiers[0]],section='health',content='已更正'))
        self.assertEqual(moved['section'],'health')
        self.assertEqual(moved['created_at'],first['created_at'])
        other = BookmarkStore(self.app,'two').execute(dict(action='delete',ids=[identifiers[0]]))
        self.assertFalse(other['ok'])
        result = self.bookmarks.execute(dict(action='delete',ids=[identifiers[0],identifiers[0],'12345678','ffffffff']))
        self.assertEqual(result['deleted'],['12345678'])
        self.assertEqual(result['ambiguous'],['12345678'])
        self.assertEqual(result['not_found'],['ffffffff'])
        self.assertEqual(self.bookmarks.list()['total'],1)
        for args in [dict(action='update',ids=[],content='新'),dict(action='update',ids=['1234'],content='新'),dict(content='无分区'),dict(section='daily',content='\n '),dict(action='bad')]:
            with self.assertRaises(ValueError):
                self.bookmarks.execute(args)

    def test_business_day_boundary_newest_first_catchup_and_today_left_pending(self):
        self.notes.save({'daily':'原来的条目'},0)
        self.add('前天','2026-11-07T20:00:00+08:00')
        self.add('昨天','2026-11-08T20:00:00+08:00')
        self.add('凌晨仍属昨天','2026-11-09T02:59:59+08:00')
        current = self.add('今天','2026-11-09T03:00:00+08:00')
        task = self.task()
        self.notes.absorb(task,now=NOW)
        notes = self.notes.read(self.policy)
        self.assertEqual(notes['sections']['daily'],'凌晨仍属昨天\n昨天\n前天\n原来的条目')
        self.assertEqual(notes['version'],2)
        self.assertEqual([row['full_id'] for row in self.bookmarks.list()['items']],[current['full_id']])
        with self.assertRaises(TaskLost):
            self.notes.absorb(task,now=NOW)
        missing = self.bookmarks.execute(dict(action='update',ids=['ffffffff'],content='新进展'))
        self.assertIn('已经整理',missing['errors'][0])

    def test_absorption_is_one_transaction_at_both_write_checkpoints(self):
        self.add()
        task = self.task()
        for stage in ('sections','bookmarks'):
            def fail(current, connection):
                if current==stage:
                    raise RuntimeError('synthetic failure')
            with patch.object(self.notes,'_checkpoint',side_effect=fail),self.assertRaises(RuntimeError):
                self.notes.absorb(task,now=NOW)
            self.assertEqual(self.notes.read(self.policy)['version'],0)
            self.assertEqual(self.bookmarks.list()['total'],1)
        self.notes.absorb(task,now=NOW)
        self.assertEqual(self.bookmarks.list()['total'],0)

    def test_version_checks_and_restart_recovery_skip_live_worker(self):
        self.notes.save({'health':'★长期用药'},0)
        with self.assertRaises(RevisionConflict):
            self.notes.save({'health':'不能覆盖'},0)
        job,_ = self.notes.begin_tidy(self.policy)
        with self.notes.tidy_lock() as locked:
            self.assertTrue(locked)
            RecentNotesStore(self.app,'one').recover()
            self.assertEqual(self.notes.read(self.policy)['tidy_job']['status'],'running')
        self.notes.recover()
        self.assertEqual(self.notes.read(self.policy)['tidy_job']['status'],'failed')
        self.assertFalse(self.notes.finish_tidy(job['id'],dict(status='done')))
        self.assertEqual(self.notes.read(self.policy)['sections']['health'],'★长期用药')


class RecentNotesUpgradeTests(DatabaseTestCase):
    def test_nondefault_timezone_controls_bookmark_business_day(self):
        install(self.owner,Settings(3,timezone='America/New_York',day_start_hour=3))
        SpaceConfiguration(self.owner).configure('one',Participant('用户'),Participant('助手'))
        store = BookmarkStore(self.app,'one')
        first = store.execute(dict(section='daily',content='边界前'),now=datetime.fromisoformat('2026-11-09T07:59:59+00:00'))
        second = store.execute(dict(section='daily',content='边界后'),now=datetime.fromisoformat('2026-11-09T08:00:00+00:00'))
        with connect(self.app) as c:
            days = {str(row['id']):row['business_date'] for row in c.execute('SELECT id,business_date FROM memory_core.moment_bookmarks')}
        self.assertEqual(days[first['full_id']],'2026-11-08')
        self.assertEqual(days[second['full_id']],'2026-11-09')
        self.assertTrue(second['created_at'].endswith('-05:00'))

    def test_v19_upgrade_repeat_and_failed_migration_rollback(self):
        install(self.owner,Settings(3),target_version=19)
        SpaceConfiguration(self.owner).configure('one',Participant('用户'),Participant('助手'))
        mid = PostgresStore(self.app,'one').write_card('保留旧卡',[1,0,0])
        with tempfile.TemporaryDirectory() as directory:
            for path in MIGRATIONS.glob('*.sql'):
                (Path(directory)/path.name).write_text(path.read_text())
            target = Path(directory)/'020_recent_notes.sql'
            target.write_text(target.read_text()+'\nSELECT 1/0;')
            with patch('memory_core.storage.install.MIGRATIONS',Path(directory)),self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner,Settings(3))
        with connect(self.app) as c:
            self.assertIsNone(c.execute("SELECT to_regclass('memory_core.recent_notes') AS name").fetchone()['name'])
            self.assertEqual(c.execute('SELECT version FROM memory_core.installation').fetchone()['version'],19)
        self.assertTrue(install(self.owner,Settings(3)))
        self.assertFalse(install(self.owner,Settings(3)))
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT content FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone()['content'],'保留旧卡')
        self.assertEqual(RecentNotesStore(self.app,'one').read(TidyPolicy())['version'],0)
