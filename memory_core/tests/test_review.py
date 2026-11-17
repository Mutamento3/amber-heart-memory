from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import psycopg

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.cards.drafts import DraftStore, context_for, structure_input
from memory_core.cards.extraction_contract import render_prompt
from memory_core.cards.graph_store import AmbiguousEntity, GraphStore
from memory_core.storage.install import install, catalog_fingerprint, MIGRATIONS
from memory_core.cards.keyword_index import build_rows, tokenize
from memory_core.cards.review import PreparationError, ReviewStore, entity_embedding_text
from memory_core.storage.space import Participant, RevisionConflict, SpaceConfiguration
from memory_core.testing.fixed_models import FixedModels
from memory_core.tests.support import DatabaseTestCase


class FaultReview(ReviewStore):
    failure = None

    def _checkpoint(self,stage,connection):
        if stage==self.failure:
            connection.execute('SELECT 1/0')


class ReviewTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        install(self.owner,Settings(768))
        self.config=SpaceConfiguration(self.owner)
        self.config.configure('example',Participant('Example User'),Participant('Example Assistant'))
        self.store=ReviewStore(self.app,'example')
        self.topic=self.store.topics()[0]['id']
        self.vector=[1.0]+[0.0]*767

    def ready(self,key='one', *, entities=True):
        first=dict(title='合成植物学习计划',content='Example User plans to study botany.',summary='合成植物学习计划',importance=8,
                   evidence=[dict(segment_id='s1',quote='plans to study botany.')])
        extraction=self.store.submit(key,'request:'+key,[dict(segment_id='s1',speaker='user',text=first['content'],timestamp='2026-06-12T10:00:00+08:00')])
        with connect(self.app) as connection: context=context_for(connection,'example')
        participant=self.store.policy()['user_entity_id']
        graph=dict(summary=first['summary'],experience_mode='external',cognitive_status='stable',decay_layer='tier_4_episode',
                   source_date='2026-06-12',is_milestone=False,valence=0.4,arousal=0.3,topic_ids=[self.topic],entities=[],relations=[])
        if entities:
            graph['entities']=[
                dict(ref='user',name='Example User',known_entity_id=participant,entity_type='PERSON',entity_subtype=None,aliases=[],mention_role='subject',description='Synthetic participant'),
                dict(ref='course',name='植物课程',known_entity_id=None,entity_type='EVENT',entity_subtype=None,aliases=['园艺课程'],mention_role='object',description='学习植物知识的合成课程'),
            ]
            graph['relations']=[dict(source_ref='user',target_ref='course',relation_type='PARTICIPATES_IN',relation_type_raw=None,description='合成学习计划',valid_from=None)]
        fixtures={'extract':{
            self.store.selection_prompt(extraction):json.dumps(dict(memories=[first],skipped_reason=None)),
            render_prompt('structure',context,structure_input(first,'2026-06-12',[dict(segment_id='s1',speaker='user',timestamp='2026-06-12T10:00:00+08:00')],self.store.settings)):json.dumps(graph),
        }}
        models=FixedModels(fixtures)
        return self.store.extract(extraction,selector=models,structurer=models)['drafts'][0]

    def embeddings(self,draft, *, vector=None):
        values={draft['payload']['summary']:self.vector if vector is None else vector}
        with connect(self.app) as connection:
            context=context_for(connection,'example')
        participants={p['id'] for p in context['participants'].values()}
        for entity in draft['payload']['entities']:
            if entity['known_entity_id'] not in participants:
                with connect(self.app) as connection: existing=self.store._resolve(connection,entity)
                merged=deepcopy(entity)
                if existing:
                    merged.update(name=existing['name'],aliases=list(dict.fromkeys(existing['aliases']+entity['aliases'])),description=entity['description'] or existing['description'])
                values[entity_embedding_text(merged)]=self.vector
        return FixedModels({'embed':values})

    def approve(self,draft, *, store=None, models=None, key=None):
        return (store or self.store).approve(draft['id'],expected_version=draft['version'],
            expected_policy_revision=draft['policy_revision'],request_key=key or 'review:'+str(draft['id']),
            embedder=models or self.embeddings(draft),embedding_model='fixed-synthetic-v1')

    def counts(self):
        with connect(self.app) as connection:
            return {name:connection.execute('SELECT count(*) AS n FROM memory_core.'+name).fetchone()['n']
                    for name in ('memory_items','entities','memory_entities','relations','memory_topics','memory_keyword_tokens')}

    def test_complete_transaction_seven_fields_and_two_retrieval_paths(self):
        draft=self.ready()
        result=self.approve(draft)
        self.assertEqual(result['status'],'approved')
        mid=result['memory_item_id']
        self.assertEqual(self.store.search_vectors(self.vector)[0]['id'],mid)
        self.assertEqual(self.store.search_keywords('植物')[0]['memory_id'],mid)
        with connect(self.app) as connection:
            fields={r['field'] for r in connection.execute('SELECT DISTINCT field FROM memory_core.memory_keyword_tokens WHERE memory_id=%s',(mid,))}
            self.assertEqual(fields,{'title','summary','content','entity_name','entity_description','alias','topic_name'})
            card=connection.execute('SELECT * FROM memory_core.memory_items WHERE id=%s',(mid,)).fetchone()
            self.assertEqual(card['primary_topic_id'],self.topic)
            self.assertEqual(card['experience_mode'],'external')
            self.assertEqual(connection.execute('SELECT count(*) AS n FROM memory_core.relations WHERE source_memory_id=%s',(mid,)).fetchone()['n'],1)
        self.assertEqual(self.store.card_source(mid)['source_status'],'available')

    def test_each_write_phase_failure_rolls_back_everything(self):
        draft=self.ready()
        original=self.counts()
        fault=FaultReview(self.app,'example')
        for stage in ('card','entities','bridges','relations','topics','keywords','review'):
            fault.failure=stage
            with self.subTest(stage=stage),self.assertRaises(psycopg.errors.DivisionByZero): self.approve(draft,store=fault)
            self.assertEqual(self.counts(),original)
            actual=self.store.get(draft['id'])
            self.assertEqual(actual['status'],'pending')
            self.assertEqual(actual['version'],draft['version'])
            self.assertIsNone(actual['memory_item_id'])
        self.assertEqual(self.approve(draft)['status'],'approved')

    def test_required_vector_failure_precedes_writes(self):
        draft=self.ready()
        original=self.counts()
        for values in ({'error':'timeout'},[0.0]*768,[1.0], [float('nan')]+[0.0]*767):
            models=FixedModels({'embed':{draft['payload']['summary']:values}})
            with self.assertRaises(PreparationError): self.approve(draft,models=models)
            self.assertEqual(self.counts(),original)
        missing_entity=FixedModels({'embed':{draft['payload']['summary']:self.vector}})
        with self.assertRaisesRegex(PreparationError,'entity_embedding'): self.approve(draft,models=missing_entity)
        self.assertEqual(self.store.get(draft['id'])['status'],'pending')

    def test_repeated_and_concurrent_confirmation_returns_single_card(self):
        draft=self.ready()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda i:self.approve(draft,key='parallel:'+str(i)),range(2)))
        self.assertEqual(results[0]['memory_item_id'],results[1]['memory_item_id'])
        self.assertEqual(self.counts()['memory_items'],1)
        again=self.approve(draft,models=FixedModels({}),key='another-click')
        self.assertEqual(again['memory_item_id'],results[0]['memory_item_id'])

    def test_similar_drafts_from_distinct_sources_both_approve_concurrently(self):
        drafts=[self.ready('parallel-'+str(i),entities=False) for i in range(2)]
        self.assertEqual([draft['status'] for draft in drafts],['pending','pending'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(self.approve,drafts))
        self.assertEqual([r['status'] for r in results],['approved','approved'])
        self.assertNotEqual(results[0]['memory_item_id'],results[1]['memory_item_id'])
        self.assertTrue(all(r['dedup_state'] is None and r['duplicate_memory_id'] is None for r in results))
        self.assertEqual(self.counts()['memory_items'],2)

    def test_approval_prepares_card_embedding_once_even_when_vector_matches(self):
        draft=self.ready(entities=False)
        existing=self.store.write_card(draft['payload']['content'],self.vector)
        models=self.embeddings(draft)
        result=self.approve(draft,models=models)
        self.assertEqual(result['status'],'approved')
        self.assertNotEqual(result['memory_item_id'],existing)
        self.assertEqual(models.calls,[('embed',draft['payload']['summary'])])

    def test_same_review_request_cannot_approve_another_draft(self):
        first=self.ready()
        self.approve(first,key='same-request')
        second=self.ready('second')
        with self.assertRaises(ValueError): self.approve(second,key='same-request')
        self.assertEqual(self.store.get(second['id'])['status'],'pending')

    def test_historical_duplicate_rejection_remains_idempotent(self):
        draft=self.ready(entities=False)
        existing=self.store.write_card('合成旧卡',self.vector)
        with connect(self.app) as connection:
            connection.execute("""UPDATE memory_core.pending_memories
                SET status='rejected',duplicate_memory_id=%s,dedup_state='duplicate',review_key='legacy-review',
                reviewed_at=memory_core.format_timestamp(CURRENT_TIMESTAMP),version=version+1
                WHERE id=%s""",(existing,draft['id']))
        result=self.approve(draft,models=FixedModels({}),key='legacy-review')
        self.assertEqual(result['status'],'rejected')
        self.assertEqual(result['duplicate_memory_id'],existing)
        self.assertEqual(result['dedup_state'],'duplicate')
        self.assertEqual(self.counts()['memory_items'],1)

    def test_wrong_space_version_policy_and_source_changes_reject_approval(self):
        draft=self.ready()
        self.config.configure('other',Participant('Other User'),Participant('Other Assistant'))
        other=ReviewStore(self.app,'other')
        with self.assertRaises(ValueError): self.approve(draft,store=other)
        with self.assertRaises(RevisionConflict):
            self.store.approve(draft['id'],expected_version=draft['version']+1,expected_policy_revision=1,request_key='bad-version',embedder=self.embeddings(draft),embedding_model='fixed')
        self.config.set_core_memory('example',expected_revision=1,core_memory='New visible fact')
        with self.assertRaises(RevisionConflict): self.approve(draft)
        edited=self.store.edit(draft['id'],draft['payload'],expected_version=draft['version'],expected_policy_revision=2)
        self.store.delete_material(self.store.source(draft['id'])['id'])
        with self.assertRaises(ValueError): self.approve(edited)
        self.assertEqual(self.counts()['memory_items'],0)

    def test_configuration_changes_during_embedding_do_not_commit(self):
        draft=self.ready()
        base=self.embeddings(draft)
        config=self.config
        class ChangingModel:
            changed=False
            def embed(instance,value):
                if not instance.changed:
                    instance.changed=True
                    config.set_core_memory('example',expected_revision=1,core_memory='Changed during preparation')
                return base.embed(value)
        with self.assertRaises(RevisionConflict): self.approve(draft,models=ChangingModel())
        self.assertEqual(self.counts()['memory_items'],0)

    def test_entity_changes_during_embedding_require_repreparation(self):
        draft=self.ready()
        base=self.embeddings(draft)
        store=self.store
        class ChangingEntity:
            changed=False
            def embed(instance,value):
                if value.startswith('实体：') and not instance.changed:
                    instance.changed=True
                    store.create_entity('植物课程',entity_type='EVENT',description='Concurrent synthetic object')
                return base.embed(value)
        with self.assertRaises(RevisionConflict): self.approve(draft,models=ChangingEntity())
        self.assertEqual(self.counts()['memory_items'],0)
        self.assertEqual(self.store.get(draft['id'])['status'],'pending')

    def test_database_range_subtype_and_source_constraints(self):
        for sql,params in (
            ("INSERT INTO memory_core.entities(bot_id,entity_key,name,entity_type,entity_subtype) VALUES ('example','invalid','Bad','OBJECT','persona')",()),
            ("INSERT INTO memory_core.memory_items(bot_id,content,valence) VALUES ('example','Synthetic',2)",()),
            ("UPDATE memory_core.pending_memories SET status='approved' WHERE id=%s",(self.ready()['id'],)),
        ):
            with self.assertRaises(psycopg.errors.CheckViolation):
                with connect(self.app) as connection: connection.execute(sql,params)

    def test_source_delete_keeps_approved_card_and_short_evidence(self):
        draft=self.ready()
        result=self.approve(draft)
        source=self.store.card_source(result['memory_item_id'])
        self.store.delete_material(source['id'])
        self.assertEqual(self.store.card_source(result['memory_item_id'])['source_status'],'原文已删除')
        self.assertEqual(self.store.get(draft['id'])['payload']['evidence'],draft['payload']['evidence'])
        self.assertEqual(self.store.search_vectors(self.vector)[0]['id'],result['memory_item_id'])
        self.assertEqual(self.approve(draft)['memory_item_id'],result['memory_item_id'])

    def test_same_name_is_ambiguous_but_explicit_entity_reference_works(self):
        one=self.store.create_entity('植物课程',entity_type='EVENT')
        two=self.store.create_entity('植物课程',entity_type='EVENT')
        draft=self.ready()
        with self.assertRaises(AmbiguousEntity): self.approve(draft)
        payload=deepcopy(draft['payload']); payload['entities'][1]['known_entity_id']=one
        edited=self.store.edit(draft['id'],payload,expected_version=draft['version'],expected_policy_revision=1)
        result=self.approve(edited)
        with connect(self.app) as connection:
            ids={r['entity_id'] for r in connection.execute('SELECT entity_id FROM memory_core.memory_entities WHERE memory_id=%s',(result['memory_item_id'],))}
        self.assertIn(one,ids); self.assertNotIn(two,ids)

    def test_distinct_sources_keep_relation_evidence_and_alias_reuse(self):
        first=self.ready(); self.approve(first)
        second=self.ready('second')
        vector=[0.0,1.0]+[0.0]*766
        self.approve(second,models=self.embeddings(second,vector=vector))
        with connect(self.app) as connection:
            relations=connection.execute('SELECT source_memory_id FROM memory_core.relations').fetchall()
            self.assertEqual(len({r['source_memory_id'] for r in relations}),2)
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM memory_core.entities WHERE name='植物课程'").fetchone()['n'],1)

    def test_real_tokenization_keeps_seven_field_lengths_and_custom_terms(self):
        values=tokenize('植物学习和园艺课程 Example User',ignored_terms=['Example'])
        self.assertIn('植物',values)
        self.assertIn('园艺',values)
        self.assertNotIn('example',values)
        card=dict(id=1,title='植物植物',summary='植物学习',content='园艺课程')
        rows=build_rows(card,[dict(id=2,name='植物课程',description='学习植物',aliases=['园艺课程'])],[dict(id=3,name='兴趣爱好')],timestamp='2026-06-12T10:00:00+08:00')
        for field in {r['field'] for r in rows}:
            subset=[r for r in rows if r['field']==field]
            self.assertTrue(all(r['field_length']==sum(s['term_frequency'] for s in subset) for r in subset))

    def test_default_cli_approval_and_repeated_confirmation(self):
        draft=self.ready()
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'fixed.json'; file.write_text(json.dumps(self.embeddings(draft).fixtures))
            env={'PATH':os.environ['PATH'],'PYTHONDONTWRITEBYTECODE':'1','MEMORY_DSN':self.app}
            command=[sys.executable,'-m','memory_core.daily.workflow','--space','example','approve',str(draft['id']),
                '--version',str(draft['version']),'--revision','1','--request-key','cli-review','--embedding-label','fixed-synthetic-v1','--fixed-responses',str(file)]
            results=[subprocess.run(command,env=env,capture_output=True,text=True) for _ in range(2)]
            for result in results: self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(results[0].stdout)['memory_item_id'],json.loads(results[1].stdout)['memory_item_id'])

    def test_v4_upgrade_and_failed_v5_rollback_preserve_pending_draft(self):
        with connect(self.owner) as connection: connection.execute('DROP SCHEMA memory_core CASCADE')
        install(self.owner,Settings(768),target_version=4)
        self.config.configure('example',Participant('Example User'),Participant('Example Assistant'))
        store=DraftStore(self.app,'example')
        # Recreate the runtime as well: the previous one belonged to the
        # latest schema that this upgrade fixture just dropped.
        self.store=store
        self.topic=store.topics()[0]['id']
        extraction=store.submit('v4','v4',[dict(segment_id='old',speaker='user',text='synthetic old input',timestamp='2026-06-12T10:00:00+08:00')])
        old_draft=self.ready('v4-draft')
        with tempfile.TemporaryDirectory() as folder:
            for source in MIGRATIONS.glob('*.sql'):
                (Path(folder)/source.name).write_text(source.read_text()+('\nSELECT 1/0;' if source.name.startswith('005') else ''))
            with patch('memory_core.storage.install.MIGRATIONS',Path(folder)),self.assertRaises(psycopg.errors.DivisionByZero): install(self.owner,Settings(768))
        with connect(self.owner) as connection: self.assertEqual(connection.execute('SELECT version FROM memory_core.installation').fetchone()['version'],4)
        self.assertEqual(store.extraction(extraction)['status'],'pending')
        self.assertEqual(store.get(old_draft['id'])['payload'],old_draft['payload'])
        install(self.owner,Settings(768))
        self.assertEqual(store.get(old_draft['id'])['payload'],old_draft['payload'])
        with connect(self.owner) as connection: expected=catalog_fingerprint(connection)
        self.assertFalse(install(self.owner,Settings(768)))
        with connect(self.owner) as connection:
            connection.execute('DROP SCHEMA memory_core CASCADE')
        install(self.owner,Settings(768))
        with connect(self.owner) as connection: self.assertEqual(catalog_fingerprint(connection),expected)
