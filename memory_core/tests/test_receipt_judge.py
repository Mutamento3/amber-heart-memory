from datetime import datetime, timedelta
import threading
import unittest

from psycopg.types.json import Jsonb

from memory_core.lifecycle.archive_policy import ArchiveSettings, receipt_health
from memory_core.storage.config import Settings
from memory_core.conversations.conversation_rounds import lock_space_state
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.storage.postgres import PostgresStore
from memory_core.daily.receipt_judge import ReceiptJudgeFormatError, ReceiptJudgeStore, batches, material, parse
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase

NOW = datetime.fromisoformat('2026-11-05T19:00:00+08:00')
DAY = '2026-11-05'


def seed_round(dsn, cards, reply, *, space='one', moment=NOW, status='unreported', position=None,
               history='current', steps=1):
    """One finished synthetic round using the installed conversation constraints."""
    with connect(dsn) as c:
        tid = c.execute("SELECT nextval(pg_get_serial_sequence('memory_core.conversation_turns','id')) AS id").fetchone()['id']
        stamp = moment.isoformat()
        snapshot = dict(status='ok', cards=[dict(number=i + 1, memory_id=mid, title='合成标题%d' % (i + 1),
            content='合成卡片正文%d' % (i + 1)) for i, mid in enumerate(cards)])
        c.execute('''INSERT INTO memory_core.conversation_turns
            (id,bot_id,position_id,generation_state,history_state,business_date,created_at,finished_at,recall_snapshot)
            VALUES (%s,%s,%s,'complete',%s,%s,%s,%s,%s)''',
            (tid, space, position or tid, history, DAY, stamp, stamp, Jsonb(snapshot)))
        request = None
        for step in range(steps):
            request = c.execute('''INSERT INTO memory_core.conversation_requests
                (bot_id,turn_id,step_index,request_fingerprint,model,stream,state,started_at,usage_receipt)
                VALUES (%s,%s,%s,%s,'synthetic',false,'complete',%s,%s) RETURNING id''',
                (space, tid, step, '0' * 64, stamp,
                 Jsonb(dict(status=status, reported_ids=[], counted_ids=[], counted=False) if step == steps - 1
                       else dict(status='not_run', counted=False)))).fetchone()['id']
        c.execute('''INSERT INTO memory_core.conversation_messages
            (bot_id,turn_id,request_id,event_index,role,content_parts,content_state,occurred_at)
            VALUES (%s,%s,%s,0,'assistant',%s,'complete',%s)''',
            (space, tid, request, Jsonb([dict(type='text', text=reply)]), stamp))
    return tid, request


class ReceiptJudgeParseTests(unittest.TestCase):
    def test_parse_keeps_only_listed_turns_and_numbers(self):
        batch = [dict(turn_id=7, cards=[dict(number=1), dict(number=2)]), dict(turn_id=8, cards=[dict(number=1)])]
        raw = '```json\n{"results":[{"turn":7,"used":[2,2,5,"1",true]},{"turn":7,"used":[1]},{"turn":9,"used":[1]},{"turn":"8","used":[]}]}\n```'
        self.assertEqual(parse(raw, batch), {7: [1, 2], 8: []})
        for bad in ('不是 JSON', '{"result":[]}', '[]'):
            with self.subTest(bad=bad), self.assertRaises(ReceiptJudgeFormatError):
                parse(bad, batch)

    def test_only_invalid_numbers_leave_the_turn_unreported(self):
        batch = [dict(turn_id=7, cards=[dict(number=1)]), dict(turn_id=8, cards=[dict(number=1)])]
        raw = '{"results":[{"turn":7,"used":[99]},{"turn":8,"used":["一", null]}]}'
        self.assertEqual(parse(raw, batch), {})

    def test_material_is_clipped_and_batched(self):
        rounds = [dict(turn_id=i, cards=[dict(number=1, title='标题', content='正' * 1000)], reply='回' * 5000)
                  for i in range(1, 12)]
        text = material(rounds[:1])
        self.assertIn('### 轮次 1', text)
        self.assertLess(len(text), 1700)
        groups = list(batches(rounds))
        self.assertGreater(len(groups), 1)
        self.assertEqual([r['turn_id'] for g in groups for r in g], list(range(1, 12)))


class ReceiptJudgeStoreTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.core = Settings(3)
        install(self.owner, self.core)
        config = SpaceConfiguration(self.owner)
        for space in ('one', 'two'):
            config.configure(space, Participant('Synthetic Reader'), Participant('Synthetic Assistant'))
        cards = PostgresStore(self.app, 'one')
        self.cards = [cards.write_card('合成正文%d' % i, [1, 0, 0], title='合成标题%d' % i, source_date='2026-11-01')
                      for i in range(3)]
        self.store = ReceiptJudgeStore(self.app, 'one')

    def row(self, mid):
        with connect(self.app) as c:
            return c.execute('SELECT used_count,last_used_at FROM memory_core.memory_items WHERE id=%s', (mid,)).fetchone()

    def receipt(self, request):
        with connect(self.app) as c:
            return c.execute('SELECT usage_receipt FROM memory_core.conversation_requests WHERE id=%s', (request,)).fetchone()['usage_receipt']

    def test_candidates_cover_only_unreported_current_injected_rounds(self):
        wanted, _ = seed_round(self.app, self.cards[:2], '合成回复', steps=2)
        seed_round(self.app, self.cards[:1], '已报告', status='reported')
        seed_round(self.app, [], '没有注入')
        seed_round(self.app, self.cards[:1], '旧版本', history='replaced')
        seed_round(self.app, self.cards[:1], '别的空间', space='two')
        rounds = self.store.candidates(DAY)
        self.assertEqual([r['turn_id'] for r in rounds], [wanted])
        self.assertEqual(rounds[0]['reply'], '合成回复')
        self.assertEqual([c['memory_id'] for c in rounds[0]['cards']], self.cards[:2])

    def test_apply_counts_like_a_receipt_once_and_feeds_the_archive_gate(self):
        tid, request = seed_round(self.app, self.cards[:2], '合成回复')
        other, other_request = seed_round(self.app, self.cards[:1], '没用上')
        rounds = self.store.candidates(DAY)
        counts = self.store.apply(rounds, {tid: [2], other: []}, model='synthetic-judge', now=NOW, core=self.core)
        self.assertEqual(counts, dict(judged=1, judged_unused=1))
        self.assertEqual(self.receipt(request)['status'], 'judged')
        self.assertEqual(self.receipt(request)['reported_ids'], [self.cards[1]])
        self.assertEqual(self.receipt(other_request)['status'], 'judged_unused')
        self.assertEqual(self.row(self.cards[1])['used_count'], 1)
        self.assertEqual(self.row(self.cards[1])['last_used_at'], NOW.isoformat())
        self.assertEqual(self.row(self.cards[0])['used_count'], 0)
        # A second run finds nothing left and never counts twice.
        self.assertEqual(self.store.candidates(DAY), [])
        self.assertEqual(self.store.apply(rounds, {tid: [2]}, model='synthetic-judge', now=NOW, core=self.core),
                         dict(judged=0, judged_unused=0))
        self.assertEqual(self.row(self.cards[1])['used_count'], 1)
        with connect(self.app) as c:
            health = receipt_health(c, 'one', ArchiveSettings(receipt_min_samples=1), NOW + timedelta(hours=1))
        self.assertEqual((health['injected_rounds'], health['reported_rounds']), (2, 2))

    def test_same_position_versions_and_later_use_are_respected(self):
        old, _ = seed_round(self.app, self.cards[:1], '旧回复', status='reported', history='replaced')
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.conversation_requests SET usage_receipt=%s WHERE turn_id=%s",
                      (Jsonb(dict(status='reported', reported_ids=[self.cards[0]], counted_ids=[self.cards[0]], counted=True)), old))
            later = (NOW + timedelta(days=1)).isoformat()
            c.execute('UPDATE memory_core.memory_items SET used_count=1,last_used_at=%s WHERE id=%s', (later, self.cards[0]))
        tid, request = seed_round(self.app, self.cards[:2], '新回复', position=old)
        rounds = self.store.candidates(DAY)
        self.store.apply(rounds, {tid: [1, 2]}, model='synthetic-judge', now=NOW, core=self.core)
        self.assertEqual(self.receipt(request)['counted_ids'], [self.cards[1]])
        self.assertEqual(self.row(self.cards[0]), dict(used_count=1, last_used_at=later))
        self.assertEqual(self.row(self.cards[1])['used_count'], 1)

    def test_rounds_changed_after_selection_are_skipped(self):
        replaced, replaced_request = seed_round(self.app, self.cards[:1], '会被替换')
        deleted, deleted_request = seed_round(self.app, self.cards[1:2], '会被删除')
        rounds = self.store.candidates(DAY)
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.conversation_turns SET history_state='replaced' WHERE id=%s", (replaced,))
            c.execute("UPDATE memory_core.conversation_turns SET content_deleted_at=%s WHERE id=%s", (NOW.isoformat(), deleted))
        counts = self.store.apply(rounds, {replaced: [1], deleted: [1]}, model='synthetic-judge', now=NOW, core=self.core)
        self.assertEqual(counts, dict(judged=0, judged_unused=0))
        self.assertEqual(self.receipt(replaced_request)['status'], 'unreported')
        self.assertEqual(self.receipt(deleted_request)['status'], 'unreported')
        self.assertEqual([self.row(mid)['used_count'] for mid in self.cards[:2]], [0, 0])

    def test_judging_waits_for_the_live_conversation_lock(self):
        tid, request = seed_round(self.app, self.cards[:1], '合成回复')
        rounds = self.store.candidates(DAY)
        done = threading.Event()

        def judge():
            self.store.apply(rounds, {tid: [1]}, model='synthetic-judge', now=NOW, core=self.core)
            done.set()

        with connect(self.app) as c:
            lock_space_state(c, 'one')
            worker = threading.Thread(target=judge)
            worker.start()
            self.assertFalse(done.wait(0.5))
            self.assertEqual(self.receipt(request)['status'], 'unreported')
        worker.join(10)
        self.assertTrue(done.is_set())
        self.assertEqual(self.receipt(request)['status'], 'judged')
        self.assertEqual(self.row(self.cards[0])['used_count'], 1)
