from dataclasses import replace
import json
import hashlib
import math
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import psycopg

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.install import MIGRATIONS, catalog_fingerprint, install
from memory_core.parents import replace_parents
from memory_core.postgres import KeywordToken
from memory_core.query import keyword_query, keyword_prompt
from memory_core.recall_config import RecallSettings
from memory_core.search import ThreeRouteSearch, _make_route_hit, merge_and_rank
from memory_core.search_store import SearchStore
from memory_core.space import Participant, RevisionConflict, SpaceConfiguration
from memory_core.testing.fixed_models import FixedModels
from memory_core.tests.support import DatabaseTestCase


class SearchTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        install(self.owner, Settings(768))
        self.config = SpaceConfiguration(self.owner)
        self.policy = self.config.configure('garden', Participant('Example Reader', ('Reader Alias',)),
                                            Participant('Example Guide'), ignored_terms=['dear'])
        self.store = SearchStore(self.app, 'garden')
        self.vector = [1, 0] + [0] * 766
        self.sideways = [0, 1] + [0] * 766

    def card(self, text='合成植物记录', tokens=(), **kwargs):
        return self.store.write_card(text, self.vector, tokens, title=text, summary=text, **kwargs)

    def graph(self, entities=(), topics=(), **kwargs):
        return self.store.search_graph(list(entities), list(topics), revision=self.policy['revision'], **kwargs)

    def test_entity_direct_recent_five_and_count_score(self):
        entity = self.store.create_entity('Synthetic Plant')
        ids = [self.card(f'Plant {i}', source_date=f'2026-06-{i+1:02}') for i in range(9)]
        for mid in ids:
            self.store.link_entity(mid, entity)
        rows = self.graph([entity])
        self.assertEqual([r['memory_id'] for r in rows], list(reversed(ids[-5:])))
        self.assertTrue(all(abs(r['raw_score'] - 1/math.log(10)) < 1e-8 for r in rows))
        self.assertTrue(all(r['graph_source']=='entity_direct' for r in rows))

    def test_topic_direct_and_both_empty_seeds(self):
        mid = self.card()
        topic = self.store.topics()[0]['id']
        self.store.link_topic(mid, topic)
        row = self.graph(topics=[topic])[0]
        self.assertEqual((row['memory_id'], row['raw_score'], row['graph_source']), (mid, .85, 'topic_direct'))
        self.assertEqual(self.graph(), [])

    def test_relation_source_and_hop_have_different_whitelists(self):
        seed = self.store.create_entity('Synthetic Seed')
        neighbor = self.store.create_entity('Synthetic Neighbour')
        source, adjacent = self.card('Synthetic Source'), self.card('Synthetic Adjacent')
        self.store.link_entity(adjacent, neighbor)
        self.store.create_relation(seed, neighbor, 'LOVES', source_memory_id=source)
        rows = {r['memory_id']:r for r in self.graph([seed])}
        self.assertEqual(set(rows), {source})
        self.assertEqual(rows[source]['raw_score'], .75)
        self.store.create_relation(seed, neighbor, 'SUPPORTS', source_memory_id=source)
        rows = {r['memory_id']:r for r in self.graph([seed])}
        self.assertEqual(rows[adjacent]['graph_source'], 'entity_hop1')
        self.assertEqual(rows[adjacent]['raw_score'], .60)
        self.assertEqual(rows[adjacent]['neighbor_entity_id'], neighbor)
        self.assertEqual(rows[adjacent]['relation_type'], 'SUPPORTS')

    def test_relation_quotas_per_type_five_global_ten_and_dedup(self):
        seed = self.store.create_entity('Seed')
        other = self.store.create_entity('Other')
        by_type = {}
        for kind in ('LOVES','PROMISES','CONFLICTS_WITH'):
            by_type[kind] = []
            for i in range(7):
                mid = self.card(f'{kind}-{i}', source_date=f'2026-06-{i+1:02}')
                self.store.create_relation(seed, other, kind, source_memory_id=mid)
                by_type[kind].append(mid)
        rows = self.graph([seed])
        self.assertEqual(len(rows), 10)
        for kind, ids in by_type.items():
            selected = [r for r in rows if r['relation_type']==kind]
            self.assertLessEqual(len(selected), 5)
            self.assertTrue({r['memory_id'] for r in selected} <= set(ids[-5:]))
        first = rows[0]['memory_id']
        self.store.create_relation(other, seed, rows[0]['relation_type'], source_memory_id=first)
        self.assertEqual(len({r['memory_id'] for r in self.graph([seed])}), 10)

    def test_non_whitelisted_relation_does_not_expand(self):
        seed = self.store.create_entity('Seed')
        event = self.store.create_entity('Synthetic Event', entity_type='EVENT')
        mid = self.card()
        self.store.link_entity(mid, event)
        self.store.create_relation(seed, event, 'PARTICIPATES_IN', source_memory_id=mid)
        self.assertEqual(self.graph([seed]), [])

    def test_archive_filter_precedes_quota_and_participants_only_block_start(self):
        entity = self.store.create_entity('Seed')
        ids = [self.card(f'Recent {i}', source_date=f'2026-06-{i+1:02}') for i in range(8)]
        for mid in ids:
            self.store.link_entity(mid, entity)
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET archive_reason='gardener',archived_at='2026-06-10T00:00:00+08:00' WHERE id=ANY(%s)", (ids[-3:],))
        self.assertEqual({r['memory_id'] for r in self.graph([entity])}, set(ids[:5]))
        self.assertEqual(len(self.graph([entity], include_archived=True)), 5)
        participant = self.policy['user_entity_id']
        self.store.link_entity(ids[0], participant)
        self.assertEqual(self.graph([participant]), [])
        source = self.card('Relation Source')
        self.store.create_relation(entity, participant, 'SUPPORTS', source_memory_id=source)
        via_participant = self.card('Participant Neighbour Only')
        self.store.link_entity(via_participant,participant)
        rows = {r['memory_id']:r for r in self.graph([entity])}
        self.assertEqual(rows[via_participant]['graph_source'],'entity_hop1')
        self.assertEqual(rows[via_participant]['neighbor_entity_id'],participant)

    def test_graph_scope_seeds_and_revision_are_strict_in_sql(self):
        self.config.configure('other', Participant('Second Reader'), Participant('Second Guide'))
        other = SearchStore(self.app, 'other')
        eid = other.create_entity('Other Seed')
        mid = other.write_card('Other Space', self.vector)
        other.link_entity(mid, eid)
        with self.assertRaises(psycopg.Error): self.graph([eid])
        with self.assertRaises(psycopg.Error): self.graph(topics=[other.topics()[0]['id']])
        self.assertEqual(self.graph(), [])
        with connect(self.app) as c, self.assertRaises(psycopg.Error):
            c.execute("SELECT * FROM memory_core.search_graph('{}'::int[],'{}'::int[],'garden',30,'atomic',false,999)")

    def test_entities_keep_top_ten_before_participant_filter_and_point_three_threshold(self):
        for i in range(12):
            self.store.create_entity(f'Botany {i}', embedding=self.vector)
        self.store.create_entity('Orthogonal', embedding=self.sideways)
        self.assertEqual(len(self.store.search_entities(self.vector, limit=10)), 10)
        service = ThreeRouteSearch(self.store)
        service.policy = self.policy
        ids, _, debug = service._seeds('unmatched', self.vector)
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(debug['vector_entity_ids']), 10)
        self.assertNotIn(self.store.resolve_entity('Orthogonal'), ids)

    def test_default_description_seeds_and_optional_name_filter_preserve_bm25(self):
        eid = self.store.create_entity('Synthetic Lantern')
        mid = self.card(tokens=[KeywordToken('植物',field='entity_description',source_id=eid)])
        self.store.link_entity(mid,eid)
        models = FixedModels({'embed': {'植物':self.sideways}})
        stats = {}
        hits = ThreeRouteSearch(self.store).search('植物',embedder=models,cleaner=models,diagnostics=stats)
        self.assertIn(eid, stats['graph_seeds']['keyword_entity_ids'])
        filtered = {}
        other_hits = ThreeRouteSearch(self.store,replace(RecallSettings(),graph_entity_name_only_enabled=True)).search('植物',embedder=models,cleaner=models,diagnostics=filtered)
        self.assertEqual(filtered['graph_seeds']['keyword_entity_ids'], [])
        self.assertEqual(hits[0]['keyword_score_norm'], other_hits[0]['keyword_score_norm'])
        self.store.replace_keywords(mid, [KeywordToken('植物',field='alias',source_id=eid)])
        ThreeRouteSearch(self.store,replace(RecallSettings(),graph_entity_name_only_enabled=True)).search('植物',embedder=models,cleaner=models,diagnostics=filtered)
        self.assertIn(eid, filtered['graph_seeds']['keyword_entity_ids'])

    def test_name_evidence_checks_exact_memory_entity_pair(self):
        one, two = self.store.create_entity('One'),self.store.create_entity('Two')
        first = self.card(tokens=[KeywordToken('植物',field='alias',source_id=one)])
        second = self.card(tokens=[KeywordToken('植物',field='alias',source_id=two)])
        rows = [{'memory_id':first,'matched_entity_id':two},{'memory_id':second,'matched_entity_id':one}]
        self.assertEqual(self.store.name_evidence(rows,['植物']), {})

    def test_raw_query_for_embedding_cleaned_tokens_and_route_failure_isolation(self):
        entity = self.store.create_entity('Synthetic Plant')
        mid = self.card(tokens=[KeywordToken('植物',field='entity_name',source_id=entity)])
        self.store.link_entity(mid,entity)
        raw = 'dear 我还记得那次一起研究植物的事情呢'
        cleaning_key = json.dumps([raw,keyword_prompt(raw,self.policy)],ensure_ascii=False)
        models = FixedModels({'clean_query': {cleaning_key:'植物'},'embed': {raw:self.vector}})
        stats = {}
        result = ThreeRouteSearch(self.store).search(raw,embedder=models,cleaner=models,diagnostics=stats)
        self.assertEqual(result[0]['memory_id'], mid)
        self.assertEqual(models.calls, [('clean_query',cleaning_key),('embed',raw)])
        self.assertEqual(stats['graph_seeds']['keyword_terms'], ['植物'])
        for route in ('search_keywords','search_vectors','search_entities','search_graph'):
            with self.subTest(route=route), patch.object(self.store,route,side_effect=TimeoutError):
                stats={}
                self.assertTrue(ThreeRouteSearch(self.store).search(raw,embedder=models,cleaner=models,diagnostics=stats))
                self.assertEqual(stats['status'],'degraded')

    def test_embedding_down_invalid_vector_and_cleaner_down_keep_keyword(self):
        mid = self.card(tokens=[KeywordToken('植物')])
        for response in ({'error':'timeout'},None,[],[0]*768):
            models = FixedModels({'embed': {'植物':response}})
            stats={}
            rows=ThreeRouteSearch(self.store).search('植物',embedder=models,cleaner=models,diagnostics=stats)
            self.assertEqual(rows[0]['memory_id'],mid)
            self.assertEqual(stats['routes']['graph'],'skipped')
            self.assertEqual(stats['status'],'degraded')
        raw='dear 植物'
        key=json.dumps([raw,keyword_prompt(raw,self.policy)],ensure_ascii=False)
        models=FixedModels({'clean_query':{key:{'error':'timeout'}},'embed':{raw:self.vector}})
        stats={}
        self.assertTrue(ThreeRouteSearch(self.store).search(raw,embedder=models,cleaner=models,diagnostics=stats))
        self.assertEqual(stats['cleaning'],'failed_fallback')

    def test_merge_preserves_weights_normalization_negative_similarity_and_no_new_penalties(self):
        kw={1:_make_route_hit(1,4,'keyword','kw','memory'),2:_make_route_hit(2,2,'keyword','kw','memory')}
        vec={1:_make_route_hit(1,.2,'vector','vec','memory'),2:_make_route_hit(2,-.2,'vector','vec','memory')}
        graph={1:_make_route_hit(1,1,'graph','graph','entity')}
        rows={r['memory_id']:r for r in merge_and_rank(kw,vec,graph,30)}
        self.assertEqual(rows[1]['final_score'],1)
        self.assertEqual(rows[2]['final_score'],round(.25*.5-.35+.06,6))
        self.assertEqual(rows[1]['routes_hit_count'],3)

    def test_graph_disabled_and_no_candidates_never_invents_results(self):
        stats={}
        models=FixedModels({'embed':{'植物':self.vector}})
        rows=ThreeRouteSearch(self.store,replace(RecallSettings(),graph_enabled=False)).search('植物',embedder=models,cleaner=models,diagnostics=stats)
        self.assertEqual(rows,[])
        self.assertEqual(stats['status'],'empty')
        self.assertEqual(stats['routes']['graph'],'disabled')
        self.assertEqual(ThreeRouteSearch(self.store).search(' ',embedder=models,cleaner=models,diagnostics=stats),[])
        self.assertEqual(stats['status'],'skipped')

    def test_all_routes_and_merge_cap_thirty_after_space_and_archive_filtering(self):
        topic=self.store.topics()[0]['id']
        active=[]
        for i in range(36):
            mid=self.card(f'Active {i}',tokens=[KeywordToken('植物',field='topic_name',source_id=topic)])
            self.store.link_topic(mid,topic)
            active.append(mid)
        archived=[self.card(f'Archived {i}',tokens=[KeywordToken('植物')]) for i in range(8)]
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at='2026-06-12T00:00:00+08:00' WHERE id=ANY(%s)",(archived,))
        self.config.configure('other',Participant('Other Reader'),Participant('Other Guide'))
        other=SearchStore(self.app,'other')
        for i in range(35):other.write_card(f'Other {i}',self.vector,[KeywordToken('植物')])
        models=FixedModels({'embed':{'植物':self.vector}})
        stats={}
        rows=ThreeRouteSearch(self.store).search('植物',embedder=models,cleaner=models,diagnostics=stats)
        self.assertEqual(stats['candidate_counts'],dict(keyword=30,vector=30,graph=30))
        self.assertEqual(len(rows),30)
        self.assertTrue({r['memory_id'] for r in rows}<=set(active))

    def test_config_change_during_query_requires_retry(self):
        self.card()
        models=FixedModels({'embed':{'植物':self.vector}})
        def change(_):
            self.config.configure('garden',Participant('Renamed Reader'),Participant('Example Guide'),ignored_terms=['dear'],expected_revision=1)
            return self.vector
        with patch.object(models,'embed',side_effect=change), self.assertRaises(RevisionConflict):
            ThreeRouteSearch(self.store).search('植物',embedder=models,cleaner=models)

    def test_parent_replacement_dedup_highest_score_and_no_refill(self):
        parent=self.card('Synthetic Summary',item_kind='topic_summary')
        children=[self.card(f'Child {i}') for i in range(2)]
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET parent_reflection_id=%s,gardener_result='absorbed' WHERE id=ANY(%s)",(parent,children))
        candidates=[{**self.store.cards([mid])[mid],'memory_id':mid,'final_score':score,'reasons':[str(score)]}
                    for mid,score in zip(children,[.4,.8])]
        stats={}
        rows=replace_parents(self.store,candidates,stats)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['memory_id'],parent)
        self.assertEqual(rows[0]['content'],'Synthetic Summary')
        self.assertEqual(rows[0]['final_score'],.8)
        self.assertEqual(rows[0]['reasons'],['0.8'])
        self.assertEqual(stats['gardener']['matched_children'][str(parent)],children)
        self.assertEqual(stats['gardener']['merged_group_count'],1)

    def test_missing_parent_states_failure_and_retired_cards(self):
        parent=self.card('Synthetic Summary',item_kind='topic_summary')
        child,retired=self.card('Child'),self.card('Retired')
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.memory_items SET parent_reflection_id=%s WHERE id=%s",(parent,child))
            c.execute("UPDATE memory_core.memory_items SET archive_reason='gardener',archived_at='2026-06-10T00:00:00+08:00' WHERE id=%s",(retired,))
        candidates=[{**row,'memory_id':mid,'final_score':.5} for mid,row in self.store.cards([child,retired]).items()]
        original=self.store.cards
        def unavailable(ids):
            return {} if ids==[parent] else original(ids)
        with patch.object(self.store,'cards',side_effect=unavailable):
            stats={}; rows=replace_parents(self.store,candidates,stats)
            self.assertEqual([r['memory_id'] for r in rows],[child])
            self.assertEqual(stats['gardener']['missing_parent_count'],1)
        with patch.object(self.store,'cards',side_effect=TimeoutError):
            stats={};self.assertEqual(replace_parents(self.store,candidates,stats),candidates)
            self.assertIn('gardener_states',stats['errors'])

    def test_upgrade_from_v5_repeat_permissions_and_rollback(self):
        # A fresh v6 and a v5 upgrade must produce identical catalogs.
        with connect(self.owner) as c:
            fresh=catalog_fingerprint(c)
            c.execute('DROP SCHEMA memory_core CASCADE')
        install(self.owner,Settings(768),target_version=5)
        from memory_core.graph_store import GraphStore
        self.config.configure('garden',Participant('Example Reader'),Participant('Example Guide'))
        old=GraphStore(self.app,'garden').write_card('Old Card',self.vector)
        with tempfile.TemporaryDirectory() as folder:
            for file in MIGRATIONS.glob('*.sql'): shutil.copyfile(file,Path(folder)/file.name)
            file=Path(folder)/'006_graph_retrieval.sql';file.write_text(file.read_text()+'\nSELECT 1/0;')
            with patch('memory_core.install.MIGRATIONS',Path(folder)),self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner,Settings(768))
        with connect(self.owner) as c:
            self.assertEqual(c.execute('SELECT version FROM memory_core.installation').fetchone()['version'],5)
            self.assertIsNone(c.execute("SELECT to_regprocedure('memory_core.search_entities(vector,text,integer)') AS f").fetchone()['f'])
        self.assertTrue(install(self.owner,Settings(768)))
        self.assertFalse(install(self.owner,Settings(768)))
        with connect(self.owner) as c:
            self.assertEqual(catalog_fingerprint(c),fresh)
            self.assertEqual(c.execute('SELECT content FROM memory_core.memory_items WHERE id=%s',(old,)).fetchone()['content'],'Old Card')
            self.assertFalse(c.execute("SELECT has_function_privilege('memory_outsider','memory_core.search_graph(integer[],integer[],text,integer,text,boolean,bigint)','EXECUTE') AS allowed").fetchone()['allowed'])
        self.assertTrue(SearchStore(self.app,'garden').search_vectors(self.vector))
