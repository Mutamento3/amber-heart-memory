import psycopg
from psycopg.types.json import Jsonb

from memory_core.cards.cards import CardStore
from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.cards.review import ReviewStore
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.tests import test_review as fixtures
from memory_core.tests.support import DatabaseTestCase


class FaultCards(CardStore):
    failure=None

    def _checkpoint(self,stage,connection):
        if stage==self.failure:
            connection.execute('SELECT 1/0')


class CardTests(DatabaseTestCase):
    ready=fixtures.ReviewTests.ready
    embeddings=fixtures.ReviewTests.embeddings
    approve=fixtures.ReviewTests.approve

    def setUp(self):
        super().setUp()
        install(self.owner,Settings(768))
        self.config=SpaceConfiguration(self.owner)
        self.config.configure('example',Participant('Example User'),Participant('Example Assistant'))
        self.store=ReviewStore(self.app,'example')
        self.cards=CardStore(self.app,'example')
        self.topic=self.store.topics()[0]['id']
        self.vector=[1.0]+[0.0]*767
        self.draft=self.ready()
        self.mid=self.approve(self.draft)['memory_item_id']

    def card(self):
        with connect(self.app) as c:
            return c.execute('SELECT *,embedding::text AS vector FROM memory_core.memory_items WHERE id=%s',(self.mid,)).fetchone()

    def metadata(self):
        return dict(title='合成银河观测档案',topic_ids=[self.topic],is_milestone=True)

    def snapshot(self):
        with connect(self.app) as c:
            return {table:c.execute('SELECT to_jsonb(t) AS value FROM memory_core.'+table+' t ORDER BY 1').fetchall()
                    for table in ('memory_items','memory_keyword_tokens','entities','memory_entities','relations','memory_topics','pending_memories')}

    def test_metadata_reindexes_and_preserves_body_vector_and_graph(self):
        before=self.card()
        graph=self.snapshot()
        second=self.store.topics()[1]['id']
        value=self.metadata()|{'topic_ids':[second,self.topic]}
        self.cards.edit(self.mid,value)
        after=self.card()
        for field in ('content','summary','vector','source_date','experience_mode','cognitive_status','decay_layer','valence','arousal','source_kind','created_at'):
            self.assertEqual(before[field],after[field],field)
        for table in ('entities','memory_entities','relations','pending_memories'):
            self.assertEqual(self.snapshot()[table],graph[table],table)
        self.assertEqual((after['title'],after['is_milestone'],after['primary_topic_id']),(value['title'],True,second))
        self.assertEqual(self.store.search_keywords('银河')[0]['memory_id'],self.mid)
        with connect(self.app) as c:
            self.assertEqual({r['topic_id'] for r in c.execute('SELECT topic_id FROM memory_core.memory_topics WHERE memory_id=%s',(self.mid,))},{self.topic,second})
            self.assertEqual({r['field'] for r in c.execute('SELECT DISTINCT field FROM memory_core.memory_keyword_tokens WHERE memory_id=%s',(self.mid,))},
                {'title','summary','content','entity_name','entity_description','alias','topic_name'})

    def test_forbidden_fields_and_invalid_topics_leave_everything_unchanged(self):
        before=self.snapshot()
        self.config.configure('other',Participant('Other User'),Participant('Other Assistant'))
        other_topic=ReviewStore(self.app,'other').topics()[0]['id']
        # Capture after unrelated setup so rollback compares the whole installed graph.
        before=self.snapshot()
        invalid=[self.metadata()|{field:'forbidden'} for field in ('content','summary','source_date','experience_mode','entities','expected_version')]
        invalid += [self.metadata()|{'topic_ids':ids} for ids in ([],[True],[other_topic],[self.topic,self.topic],[self.topic]*4)]
        invalid += [self.metadata()|{'title':'   '}, self.metadata()|{'is_milestone':1}]
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.cards.edit(self.mid,value)
            self.assertEqual(self.snapshot(),before)
        with self.assertRaises(ValueError):
            CardStore(self.app,'other').delete(self.mid)
        self.assertEqual(self.snapshot(),before)

    def test_edit_and_delete_failures_roll_back_complete_transactions(self):
        fault=FaultCards(self.app,'example')
        before=self.snapshot()
        for stage in ('topics','keywords'):
            fault.failure=stage
            with self.subTest(stage=stage),self.assertRaises(psycopg.errors.DivisionByZero):
                fault.edit(self.mid,self.metadata())
            self.assertEqual(self.snapshot(),before)
        for stage in ('drafts','card','entities'):
            fault.failure=stage
            with self.subTest(stage=stage),self.assertRaises(psycopg.errors.DivisionByZero):
                fault.delete(self.mid)
            self.assertEqual(self.snapshot(),before)

    def test_delete_cleans_vectors_keywords_graph_and_review_copies_but_keeps_counts(self):
        duplicate=self.ready('duplicate')
        with connect(self.app) as c:
            # Historical duplicate rejections still reference the deleted card.
            c.execute('''UPDATE memory_core.pending_memories SET status='rejected',duplicate_memory_id=%s,
                dedup_state='duplicate',review_key='legacy-duplicate',
                reviewed_at=memory_core.format_timestamp(CURRENT_TIMESTAMP),version=version+1
                WHERE id=%s''',(self.mid,duplicate['id']))
            c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE id=%s",(self.mid,))
        self.cards.edit(self.mid,self.metadata())
        self.assertEqual(self.card()['archive_reason'],'decay')
        result=self.cards.delete(self.mid)
        self.assertTrue(result['deleted'])
        with connect(self.app) as c:
            for table,column in [('memory_items','id'),('memory_keyword_tokens','memory_id'),('memory_entities','memory_id'),('memory_topics','memory_id'),('relations','source_memory_id')]:
                self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.'+table+' WHERE '+column+'=%s',(self.mid,)).fetchone()['n'],0)
            rows=c.execute('SELECT * FROM memory_core.pending_memories ORDER BY id').fetchall()
            self.assertEqual(len(rows),2)
            for row in rows:
                self.assertEqual(row['status'],'deleted')
                self.assertEqual(row['narrative'],{})
                for key in ('payload','memory_item_id','duplicate_memory_id','policy_snapshot'):
                    self.assertIsNone(row[key])
                self.assertEqual(row['business_date'],'2026-06-12')
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.entities').fetchone()['n'],2)
        self.assertEqual(self.store.search_vectors(self.vector),[])
        self.assertEqual(self.store.search_keywords('植物'),[])
        self.assertEqual(self.approve(self.draft)['status'],'deleted')
        from memory_core.cards.daily_cards import terminal_drafts
        self.assertTrue(terminal_drafts(self.store.extraction(self.draft['extraction_id'])))
        with self.assertRaisesRegex(ValueError,'只保留计数'):
            self.store.source(self.draft['id'])

    def test_shared_entities_and_relation_endpoints_are_preserved(self):
        other=self.store.write_card('另一张合成卡片',self.vector)
        with connect(self.app) as c:
            course=c.execute("SELECT id FROM memory_core.entities WHERE name='植物课程'").fetchone()['id']
        self.store.link_entity(other,course)
        lonely=self.store.create_entity('合成孤立物品',entity_type='OBJECT')
        self.store.link_entity(self.mid,lonely)
        linked_only=self.store.create_entity('另一份关系仍使用的合成物品',entity_type='OBJECT')
        user=self.store.policy()['user_entity_id']
        with connect(self.app) as c:
            for source,tag in ((self.mid,'one'),(other,'two')):
                c.execute('''INSERT INTO memory_core.relations(bot_id,relation_key,source_entity_id,target_entity_id,relation_type,source_memory_id)
                    VALUES ('example',%s,%s,%s,'RELATES_TO',%s)''',(tag,user,linked_only,source))
        self.cards.delete(self.mid)
        with connect(self.app) as c:
            remaining={r['id'] for r in c.execute('SELECT id FROM memory_core.entities')}
            self.assertIn(course,remaining)
            self.assertIn(linked_only,remaining)
            self.assertNotIn(lonely,remaining)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM memory_core.relations WHERE source_memory_id=%s',(other,)).fetchone()['n'],1)

    def test_pending_review_entity_reference_is_preserved(self):
        pending=self.ready('still-waiting')
        with connect(self.app) as c:
            course=c.execute("SELECT id FROM memory_core.entities WHERE name='植物课程'").fetchone()['id']
        payload=pending['payload']
        payload['entities'][1]['known_entity_id']=course
        pending=self.store.edit(pending['id'],payload,expected_version=pending['version'],expected_policy_revision=pending['policy_revision'])
        self.cards.delete(self.mid)
        with connect(self.app) as c:
            self.assertIsNotNone(c.execute('SELECT id FROM memory_core.entities WHERE id=%s',(course,)).fetchone())
        self.assertEqual(self.approve(pending)['status'],'approved')

    def test_parent_summary_stays_byte_for_byte_and_is_read_only(self):
        parent=self.store.write_card('合成主题总结，记录原来的经历。',self.vector,item_kind='topic_summary')
        with connect(self.app) as c:
            c.execute('UPDATE memory_core.memory_items SET parent_reflection_id=%s WHERE id=%s',(parent,self.mid))
            c.execute('UPDATE memory_core.memory_items SET child_episode_ids=%s WHERE id=%s',(Jsonb([self.mid]),parent))
            before=c.execute('SELECT to_jsonb(m) AS value FROM memory_core.memory_items m WHERE id=%s',(parent,)).fetchone()
        for method in (lambda:self.cards.edit(parent,self.metadata()),lambda:self.cards.delete(parent)):
            with self.assertRaisesRegex(ValueError,'只读'):
                method()
        self.cards.edit(self.mid,self.metadata())
        self.cards.delete(self.mid)
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT to_jsonb(m) AS value FROM memory_core.memory_items m WHERE id=%s',(parent,)).fetchone(),before)

    def test_deleted_state_database_constraint_rejects_any_surviving_copy(self):
        for update in ("status='deleted'", "status='deleted',memory_item_id=NULL", "status='deleted',memory_item_id=NULL,payload=NULL"):
            with self.subTest(update=update),self.assertRaises(psycopg.errors.CheckViolation):
                with connect(self.app) as c:
                    c.execute('UPDATE memory_core.pending_memories SET '+update+' WHERE id=%s',(self.draft['id'],))
        self.assertEqual(self.store.get(self.draft['id'])['status'],'approved')

    def test_known_limit_reextraction_can_recreate_a_deleted_card(self):
        self.cards.delete(self.mid)
        again=self.ready('reimport-same-original')
        result=self.approve(again)
        self.assertEqual(result['status'],'approved')
        self.assertNotEqual(result['memory_item_id'],self.mid)


class CardMigrationTests(DatabaseTestCase):
    def test_v14_upgrade_preserves_existing_rows_and_adds_only_constraints(self):
        install(self.owner,Settings(3),target_version=14)
        SpaceConfiguration(self.owner).configure('example',Participant('Example User'),Participant('Example Assistant'))
        store=ReviewStore(self.app,'example')
        mid=store.write_card('合成升级前卡片',[1,0,0])
        with connect(self.app) as c:
            before=c.execute('SELECT to_jsonb(m) AS value FROM memory_core.memory_items m WHERE id=%s',(mid,)).fetchone()
            columns=c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='memory_core' AND table_name IN ('memory_items','pending_memories') ORDER BY table_name,ordinal_position").fetchall()
        with self.assertRaisesRegex(ValueError,'版本 15'):
            CardStore(self.app,'example')
        install(self.owner,Settings(3),target_version=15)
        self.assertFalse(install(self.owner,Settings(3),target_version=15))
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT to_jsonb(m) AS value FROM memory_core.memory_items m WHERE id=%s',(mid,)).fetchone(),before)
            self.assertEqual(c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='memory_core' AND table_name IN ('memory_items','pending_memories') ORDER BY table_name,ordinal_position").fetchall(),columns)
        CardStore(self.app,'example').delete(mid)
