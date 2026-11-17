from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.cards.drafts import context_for, structure_input
from memory_core.cards.extraction_contract import render_prompt
from memory_core.storage.install import install
from memory_core.storage.parents import replace_parents
from memory_core.storage.postgres import KeywordToken
from memory_core.recall.query import keyword_prompt
from memory_core.recall.recall import RecallService
from memory_core.recall.recall_config import RecallSettings
from memory_core.recall.rerank_context import TailContext, address_terms, assistant_tail, entity_terms, literal_matches
from memory_core.recall.rerank_evidence import add_rerank_evidence
from memory_core.recall.rerank_input import format_document, ranking_query, selection_prompt
from memory_core.cards.review import ReviewStore
from memory_core.recall.search import ThreeRouteSearch
from memory_core.recall.search_store import SearchStore
from memory_core.storage.space import Participant, RevisionConflict, SpaceConfiguration
from memory_core.testing.fixed_models import FixedModels
from memory_core.tests.support import DatabaseTestCase


class RecallTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        install(self.owner,Settings(768))
        self.config = SpaceConfiguration(self.owner)
        self.policy = self.config.configure('example',Participant('Example Reader',('Reader Alias',)),
            Participant('Example Guide',('Guide Alias',)),ignored_terms=['dear'])
        self.store = SearchStore(self.app,'example')
        self.vector = [1]+[0]*767
        self.settings = RecallSettings()
        self.service = RecallService(self.store)

    def cards(self, count=12):
        return [self.store.write_card(f'Synthetic plant content {i}',self.vector,
            [KeywordToken('植物')],title=f'Synthetic Plant {i}',summary=f'Synthetic Summary {i}') for i in range(count)]

    def candidates(self, query='植物', settings=None):
        models=FixedModels({'embed':{query:self.vector}})
        stats={}
        rows=ThreeRouteSearch(self.store,settings).search(query,embedder=models,cleaner=models,diagnostics=stats)
        sources=rows
        rows=replace_parents(self.store,rows,stats)
        return add_rerank_evidence(rows,sources,stats,settings or self.settings)

    def models(self, rows, *, query='植物', positions=None, selected='1,2,3,4,5', llm_query=None, order_error=None, full=True):
        order=list(reversed(range(len(rows)))) if positions is None else positions
        key=json.dumps([ranking_query(query),[r['memory_id'] for r in rows],[format_document(r) for r in rows]],ensure_ascii=False)
        response=order if order_error is None else {'error':order_error}
        ordered=[rows[i] for i in order] if order_error is None else rows
        if not full:
            ordered=ordered[:10]
        sq=query if llm_query is None else llm_query
        select_key=json.dumps([sq,[r['memory_id'] for r in ordered],selection_prompt(sq,ordered)],ensure_ascii=False)
        return FixedModels({'embed':{query:self.vector},'rerank':{key:response},'select':{select_key:selected}}),ordered

    def run_recall(self, models, **kwargs):
        return self.service.recall(kwargs.pop('query','植物'),embedder=models,cleaner=models,reranker=models,selector=models,**kwargs)

    def test_complete_order_reaches_last_candidate_and_keeps_ids_content(self):
        self.cards()
        rows=self.candidates()
        models,ordered=self.models(rows,selected='12,1,3,5,7')
        result=self.run_recall(models)
        self.assertEqual(result['retrieval_stats']['status'],'ok')
        self.assertEqual([r['memory_id'] for r in result['final']], [ordered[i]['memory_id'] for i in [11,0,2,4,6]])
        self.assertEqual(len(result['ordered']),12)
        self.assertEqual(result['ordering_stats']['ordered_ids'],[r['memory_id'] for r in ordered])
        self.assertTrue(all(r['content'].startswith('Synthetic plant content') for r in result['final']))
        self.assertEqual([c[0] for c in models.calls],['embed','rerank','select'])

    def test_ordering_never_skips_small_nonempty_input_final_skips_at_five(self):
        self.cards(3)
        rows=self.candidates()
        models,ordered=self.models(rows)
        result=self.run_recall(models)
        self.assertEqual([r['memory_id'] for r in result['final']], [r['memory_id'] for r in ordered])
        self.assertFalse(result['ordering_stats']['skipped'])
        self.assertTrue(result['selection_stats']['skipped'])
        self.assertEqual([c[0] for c in models.calls],['embed','rerank'])

    def test_empty_candidates_no_model_selection_or_invented_results(self):
        models=FixedModels({'embed':{'植物':self.vector}})
        result=self.run_recall(models)
        self.assertEqual(result['final'],[])
        self.assertEqual(result['retrieval_stats']['status'],'empty')
        self.assertEqual(models.calls,[('embed','植物')])

    def test_final_short_duplicate_out_of_range_empty_and_timeout_fall_back_to_ordered_five(self):
        self.cards(8)
        rows=self.candidates()
        for response in ('8,8,7,999,0','8,7,6,5','', 'nonsense', {'error':'timeout'}):
            with self.subTest(response=response):
                models,ordered=self.models(rows,selected=response)
                result=self.run_recall(models)
                self.assertEqual([r['memory_id'] for r in result['final']], [r['memory_id'] for r in ordered[:5]])
                self.assertEqual(result['selection_stats']['fallback'],'document_top5')
                self.assertEqual(result['retrieval_stats']['status'],'degraded')
                self.assertNotIn('raw_output',result['selection_stats'])

    def test_final_ignores_duplicate_and_invalid_tokens_when_five_valid_remain(self):
        self.cards(8)
        rows=self.candidates()
        models,ordered=self.models(rows,selected='999,8，8,6,4,2,1')
        result=self.run_recall(models)
        self.assertEqual(result['selection_stats']['valid_selected'],5)
        self.assertEqual([r['memory_id'] for r in result['final']], [ordered[i]['memory_id'] for i in [7,5,3,1,0]])
        self.assertEqual(result['retrieval_stats']['status'],'ok')

    def test_incomplete_order_uses_all_coarse_candidates_and_healthy_final_selector(self):
        self.cards(12)
        rows=self.candidates()
        models,_=self.models(rows,positions=list(range(12)),selected='12,10,8,6,4')
        order_key=next(iter(models.fixtures['rerank']))
        for response in (list(range(11)),[0,0,999],[],{'error':'timeout'}):
            with self.subTest(response=response):
                models.fixtures['rerank'][order_key]=response
                result=self.run_recall(models)
                self.assertEqual(result['ordered'],[])
                self.assertEqual([r['memory_id'] for r in result['final']],[rows[i]['memory_id'] for i in [11,9,7,5,3]])
                self.assertEqual(result['retrieval_stats']['status'],'degraded')

    def test_both_rerankers_fail_returns_coarse_top_five(self):
        self.cards(8)
        rows=self.candidates()
        models,_=self.models(rows,selected={'error':'timeout'},order_error='timeout')
        result=self.run_recall(models)
        self.assertEqual([r['memory_id'] for r in result['final']],[r['memory_id'] for r in rows[:5]])
        self.assertEqual(result['selection_stats']['fallback'],'coarse_top5')

    def test_source_lookup_failure_keeps_cards_and_marks_unavailable(self):
        self.cards(3)
        rows=self.candidates()
        models,_=self.models(rows)
        with patch.object(self.store,'source_references',side_effect=TimeoutError):
            result=self.run_recall(models)
        self.assertEqual(len(result['final']),3)
        self.assertEqual(result['final'][0]['citations'][0]['source_status'],'unavailable')
        self.assertEqual(result['retrieval_stats']['status'],'degraded')

    def test_legacy_mode_ten_candidates_no_tail_and_old_partial_fill(self):
        self.cards(12)
        settings=replace(self.settings,full_list_enabled=False)
        self.service=RecallService(self.store,settings)
        rows=self.candidates(settings=settings)
        models,ordered=self.models(rows,full=False,selected='10,9')
        result=self.run_recall(models,messages=[{'role':'assistant','content':'你还想了解哪种植物？'}])
        self.assertEqual([r['memory_id'] for r in result['final']], [ordered[i]['memory_id'] for i in [9,8,0,1,2]])
        self.assertEqual(result['retrieval_stats']['rerank_context']['reason'],'feature_disabled')
        self.assertNotIn('fallback',result['selection_stats'])

    def test_legacy_order_failure_passes_coarse_fifteen_to_final(self):
        self.cards(17)
        self.service=RecallService(self.store,replace(self.settings,full_list_enabled=False))
        rows=self.candidates()
        models,_=self.models(rows,order_error='timeout')
        final=rows[:15]
        key=json.dumps(['植物',[r['memory_id'] for r in final],selection_prompt('植物',final)],ensure_ascii=False)
        models.fixtures['select']={key:'15,13,11,9,7'}
        result=self.run_recall(models)
        self.assertEqual([r['memory_id'] for r in result['final']], [final[i]['memory_id'] for i in [14,12,10,8,6]])

    def test_rerank_explicitly_disabled_no_model_calls_or_tail(self):
        self.cards(8)
        rows=self.candidates()
        models=FixedModels({'embed':{'植物':self.vector}})
        result=self.run_recall(models,rerank=False)
        self.assertEqual([r['memory_id'] for r in result['final']],[r['memory_id'] for r in rows[:5]])
        self.assertEqual(models.calls,[('embed','植物')])
        self.assertEqual(result['ordering_stats']['reason'],'disabled')

    def test_tail_only_reaches_final_selector_and_not_embedding_ordering_or_content(self):
        self.cards(8)
        query='记得吗'
        # A vector seed must not count as a keyword hit in the old tail gate.
        self.store.create_entity('Vector Only',embedding=self.vector)
        rows=self.candidates(query)
        messages=[{'role':'assistant','content':'先观察。你想先研究哪种植物？'}]
        expected=query+'\n\n【上一条回复结尾，仅作承接参考】\n先观察。你想先研究哪种植物？\n只用于理解当前消息在回应什么，不得单独据此选记忆。若当前消息换了话题，以当前消息为准。'
        models,_=self.models(rows,query=query,llm_query=expected)
        result=self.run_recall(models,query=query,messages=messages)
        self.assertEqual(result['retrieval_stats']['status'],'ok')
        self.assertTrue(result['retrieval_stats']['rerank_context']['tail_used'])
        self.assertEqual(models.calls[0],('embed',query))
        self.assertNotIn('上一条回复',models.calls[1][1])
        self.assertIn('上一条回复',models.calls[2][1])
        self.assertTrue(all('上一条回复' not in r['content'] for r in result['final']))

    def test_parent_evidence_bounded_actual_matches_and_full_content_unchanged(self):
        parent=self.store.write_card('S'*800,self.vector,item_kind='topic_summary',title='Synthetic Summary')
        children=self.cards(5)
        with connect(self.app) as c:
            c.execute('UPDATE memory_core.memory_items SET parent_reflection_id=%s WHERE id=ANY(%s)',(parent,children))
        rows=self.candidates()
        self.assertEqual(len(rows),1)
        evidence=rows[0]['_rerank_evidence']
        self.assertLessEqual(len(evidence),480)
        self.assertEqual(evidence.count('来源 M-'),3)
        self.assertEqual(rows[0]['content'],'S'*800)
        document=format_document(rows[0])
        self.assertNotIn('S'*601,document)
        self.assertIn(evidence,document)
        prompt=selection_prompt('植物',rows)
        self.assertIn('S'*800,prompt)
        models,_=self.models(rows)
        result=self.run_recall(models)
        self.assertEqual(result['final'][0]['content'],'S'*800)
        self.assertNotIn('_rerank_evidence',result['final'][0])
        self.assertEqual(set(result['final'][0]['matched_child_ids']),set(children))
        self.assertEqual(result['retrieval_stats']['rerank_evidence']['summary_count'],1)

    def test_evidence_limits_and_disable_do_not_change_parent_identity_or_score(self):
        candidate={'memory_id':100,'content':'Official Summary','item_kind':'topic_summary','final_score':.7}
        sources=[dict(memory_id=i,final_score=i, title='T'*80,summary=' S '*100) for i in range(1,6)]
        stats={'gardener':{'matched_children':{'100':[1,2,3,4,5,999]}}}
        short=replace(self.settings,evidence_max_total_chars=90)
        result=add_rerank_evidence([candidate],sources,stats,short)[0]
        self.assertLessEqual(len(result['_rerank_evidence']),90)
        self.assertIn('M-5',result['_rerank_evidence'])
        self.assertNotIn('M-999',result['_rerank_evidence'])
        self.assertEqual(result['content'],candidate['content'])
        self.assertEqual(result['final_score'],.7)
        for config in (replace(self.settings,evidence_enabled=False),replace(self.settings,evidence_max_children=0)):
            self.assertEqual(add_rerank_evidence([candidate],sources,stats,config),[candidate])

    def test_old_gate_participant_names_aliases_vocatives_and_renamed_policy(self):
        context=TailContext(self.store)
        messages=[{'role':'assistant','content':'你想先聊哪件事情？'}]
        for query in ('Example Reader','Reader Alias','Example Guide','Guide Alias','dear'):
            with self.subTest(query=query):
                _,stats=context.build(query,messages,self.policy,self.settings,[])
                self.assertTrue(stats['tail_used'])
        # Reusing the same context must load the new revision, not keep the old
        # participant/vocative exclusions from its five-minute entity cache.
        policy=self.config.configure('example',Participant('New Scholar',('Scholar Two',)),Participant('New Guide'),
            ignored_terms=['sweetie'],expected_revision=1)
        former=self.store.create_entity('Example Reader')
        _,stats=context.build('Example Reader',messages,policy,self.settings,[])
        self.assertFalse(stats['tail_used'])
        self.assertEqual(stats['reason'],'concrete_entity')
        self.assertEqual(stats['literal_matches'][0]['entity_id'],former)
        for query in ('New Scholar','Scholar Two','New Guide','sweetie'):
            with self.subTest(query=query):
                self.assertTrue(context.build(query,messages,policy,self.settings,[])[1]['tail_used'])

    def test_old_gate_entities_body_terms_keyword_metadata_and_rhetorical_questions(self):
        eid=self.store.create_entity('Synthetic Botany',aliases=['植物计划'])
        context=TailContext(self.store)
        messages=[{'role':'assistant','content':'你想先研究什么？'}]
        for query in ('Synthetic Botany','植物计划','屁股'):
            _,stats=context.build(query,messages,self.policy,self.settings,[])
            self.assertEqual(stats['reason'],'concrete_entity')
        self.assertEqual(context.build('嗯',messages,self.policy,self.settings,[eid])[1]['reason'],'keyword_entity')
        self.assertEqual(context.build('嗯',messages,self.policy,self.settings,[987654])[1]['reason'],'keyword_entity')
        self.assertEqual(context.build('嗯',messages,self.policy,self.settings,None)[1]['reason'],'keyword_entities_unavailable')
        self.assertTrue(context.build('嗯',messages,self.policy,self.settings,[self.policy['user_entity_id']])[1]['tail_used'])
        for text in ('嗯？','知道了吗？','好不好？','你听懂了没有？'):
            # The last case includes a subject and is not an independent
            # rhetorical suffix in the existing rule; keep that distinction.
            if text.startswith('你'):
                continue
            self.assertEqual(context.build('嗯',[{'role':'assistant','content':text}],self.policy,self.settings,[])[1]['reason'],'no_substantive_question')

    def test_gate_lookup_failure_and_disabled_gate(self):
        context=TailContext(self.store)
        messages=[{'role':'assistant','content':'你想研究什么？'}]
        with patch.object(self.store,'entities',side_effect=TimeoutError):
            self.assertEqual(context.build('嗯',messages,self.policy,self.settings,[])[1]['reason'],'literal_entities_unavailable')
            settings=replace(self.settings,tail_gate_enabled=False)
            self.assertTrue(context.build('嗯',messages,self.policy,settings,None)[1]['tail_used'])

    def test_exact_gate_remains_opt_in_and_refreshes_renamed_aliases(self):
        eid=self.store.create_entity('Synthetic Tree',aliases=['Old Tree'])
        context=TailContext(self.store)
        messages=[{'role':'assistant','content':'你想研究什么？'}]
        settings=replace(self.settings,exact_entity_gate_enabled=True)
        self.assertFalse(context.build('Old Tree',messages,self.policy,settings,[])[1]['tail_used'])
        with connect(self.app) as c:
            c.execute("UPDATE memory_core.entities SET aliases=ARRAY['New Tree'] WHERE id=%s",(eid,))
        self.assertTrue(context.build('Old Tree',messages,self.policy,settings,[])[1]['tail_used'])
        self.assertFalse(context.build('New Tree',messages,self.policy,settings,[eid])[1]['tail_used'])
        self.assertTrue(context.build('嗯',messages,self.policy,settings,[eid])[1]['tail_used'])

    def test_tail_prepared_but_not_used_when_final_selector_skips(self):
        self.cards(2)
        query='记得吗'
        rows=self.candidates(query)
        models,_=self.models(rows,query=query)
        result=self.run_recall(models,query=query,messages=[{'role':'assistant','content':'你想研究什么？'}])
        context=result['retrieval_stats']['rerank_context']
        self.assertTrue(context['tail_prepared'])
        self.assertFalse(context['tail_used'])

    def test_configuration_change_during_selection_discards_results(self):
        self.cards(8)
        rows=self.candidates()
        models,_=self.models(rows)
        def change(*args,**kwargs):
            self.config.configure('example',Participant('New Reader'),Participant('New Guide'),expected_revision=1)
            return '1,2,3,4,5'
        with patch.object(models,'select',side_effect=change),self.assertRaises(RevisionConflict):
            self.run_recall(models)

    def test_cli_defaults_without_credentials_and_missing_query_argument(self):
        command=[sys.executable,'-m','memory_core.recall.recall']
        env={k:v for k,v in os.environ.items() if k in ('PATH','PYTHONPATH','LANG')}
        result=subprocess.run(command+['--show-defaults'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        defaults=json.loads(result.stdout)
        self.assertIs(defaults['MEMORY_RERANK_FULL_LIST_ENABLED'],True)
        bad = subprocess.run(
            command + ["--space", "example"], env=env, capture_output=True, text=True
        )
        self.assertEqual(bad.returncode,2)
        self.assertIn("--query", bad.stderr)

    def test_default_cli_configures_http_models_and_preserves_missing_role_fallbacks(
        self,
    ):
        from memory_core.testing.provider_server import ProviderServer

        self.cards(8)
        with ProviderServer(768) as server:
            env = {
                k: v
                for k, v in os.environ.items()
                if k in ("PATH", "PYTHONPATH", "LANG")
            }
            env.update(
                MEMORY_DSN=self.app,
                MEMORY_UPSTREAM_BASE_URL=server.url,
                MEMORY_UPSTREAM_API_KEY="synthetic-key",
                MEMORY_EMBEDDING_MODEL="synthetic-embedding",
                MEMORY_DOCUMENT_RERANK_BASE_URL=server.url,
                MEMORY_DOCUMENT_RERANK_API_KEY="synthetic-voyage",
                MEMORY_DOCUMENT_RERANK_MODEL="synthetic-reranker",
                MEMORY_FINAL_RERANK_MODEL="synthetic-final",
            )
            command = [
                sys.executable,
                "-m",
                "memory_core.recall.recall",
                "--space",
                "example",
                "--query",
                "植物",
            ]
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)["final"]), 5)
            self.assertEqual(
                [r["path"] for r in server.requests],
                [
                    "/v1/embeddings",
                    "/v1/embeddings",
                    "/v1/rerank",
                    "/v1/chat/completions",
                ],
            )
            server.requests.clear()
            server.vector = [1, 0]
            mismatch = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(mismatch.returncode, 2)
            self.assertIn("2 维，记忆库安装为 768 维", mismatch.stderr)
            server.requests.clear()
            for key in list(env):
                if key.startswith(
                    (
                        "MEMORY_EMBEDDING_",
                        "MEMORY_DOCUMENT_RERANK_",
                        "MEMORY_FINAL_RERANK_",
                    )
                ):
                    del env[key]
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)["final"]), 5)
            self.assertEqual(server.requests, [])

    def test_material_edit_approve_recall_cli_and_deleted_source_on_fresh_and_upgrade(self):
        for start_version in (None,5):
            with self.subTest(start_version=start_version):
                if start_version:
                    with connect(self.owner) as c:c.execute('DROP SCHEMA memory_core CASCADE')
                    install(self.owner,Settings(768),target_version=start_version)
                    self.config.configure('example',Participant('Example Reader',('Reader Alias',)),Participant('Example Guide',('Guide Alias',)),ignored_terms=['dear'])
                    install(self.owner,Settings(768))
                self._material_cli_chain()

    def _material_cli_chain(self):
        store=ReviewStore(self.app,'example')
        first=dict(title='Synthetic Botany',content='Example Reader will study botany.',summary='A study plan.',importance=8,
                   evidence=[dict(segment_id='s1',quote='will study botany.')])
        segments=[dict(segment_id='s1',speaker='user',text=first['content'],timestamp='2026-06-12T10:00:00+08:00')]
        extraction=store.submit('synthetic-source','synthetic-request',segments)
        with connect(self.app) as c:context=context_for(c,'example')
        graph=dict(summary=first['summary'],experience_mode='external',cognitive_status='stable',decay_layer='tier_4_episode',
                   source_date='2026-06-12',is_milestone=False,valence=None,arousal=None,entities=[],relations=[],topic_ids=[store.topics()[0]['id']])
        models=FixedModels({'extract':{
            store.selection_prompt(extraction):json.dumps({'memories':[first],'skipped_reason':None}),
            render_prompt('structure',context,structure_input(first,'2026-06-12',segments,store.settings)):json.dumps(graph)},
            'embed':{first['summary']:self.vector}})
        draft=store.extract(extraction,selector=models,structurer=models)['drafts'][0]
        draft=store.edit(draft['id'],draft['payload']|{'title':'Reviewed Botany'},expected_version=draft['version'],expected_policy_revision=1)
        saved=store.approve(draft['id'],expected_version=draft['version'],expected_policy_revision=1,
            request_key='synthetic-review',embedder=models,embedding_model='fixed-synthetic-v1')
        search=SearchStore(self.app,'example')
        rows=ThreeRouteSearch(search).search('botany',embedder=FixedModels({'embed':{'botany':self.vector}}),cleaner=models)
        fixed,ordered=self.models(rows,query='botany')
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'fixed.json';file.write_text(json.dumps(fixed.fixtures))
            args=[sys.executable,'-m','memory_core.recall.recall','--space','example','--query','botany','--fixed-responses',str(file)]
            env={k:v for k,v in os.environ.items() if k in ('PATH','PYTHONPATH','LANG')}
            env['MEMORY_DSN']=self.app
            result=subprocess.run(args,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            reply=json.loads(result.stdout)
            card=reply['final'][0]
            self.assertEqual(card['memory_id'],saved['memory_item_id'])
            self.assertEqual(card['title'],'Reviewed Botany')
            self.assertEqual(card['citations'][0]['source_status'],'available')
            self.assertEqual(card['citations'][0]['evidence'],first['evidence'])
            self.assertNotIn('segments',card['citations'][0])
            store.delete_material(store.extraction(extraction)['material_id'])
            result=subprocess.run(args,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['final'][0]['citations'][0]['source_status'],'deleted')


class RecallUnitTests(unittest.TestCase):
    def test_assistant_tail_nearest_only_cleaning_and_bounds(self):
        old={'role':'assistant','content':'以前要研究什么？'}
        self.assertEqual(assistant_tail([old,{'role':'assistant','content':None}]),'')
        self.assertEqual(assistant_tail([old,{'role':'assistant','content':'【调用工具】只有工具'}]),'')
        text='【调用工具】说明\n---\n第一句。（动作[语音]）第二句！你想研究什么？'
        self.assertEqual(assistant_tail([{'role':'assistant','content':text}]),'第二句！你想研究什么？')
        self.assertEqual(len(assistant_tail([{'role':'assistant','content':'字'*200+'？'}])),120)

    def test_short_terms_require_whole_words_and_ascii_boundaries(self):
        terms=entity_terms([{'id':1,'name':'掉发','aliases':[]},{'id':2,'name':'Tree','aliases':[]}],set())
        self.assertFalse(literal_matches('扣掉发现',terms,set()))
        self.assertFalse(literal_matches('street',terms,set()))
        self.assertTrue(literal_matches('Tree',terms,set()))

    def test_atomic_body_prefix_and_summary_full_text(self):
        atomic={'memory_id':1,'item_kind':'atomic','content':'X'*201,'title':'A'}
        self.assertNotIn('X'*201,selection_prompt('query',[atomic]))
        self.assertIn('X'*200,selection_prompt('query',[atomic]))
        self.assertIn('X'*201,selection_prompt('query',[atomic|{'item_kind':'topic_summary'}]))

    def test_defaults_come_from_one_definition_and_env_absence_keeps_true(self):
        defaults=RecallSettings()
        self.assertEqual(RecallSettings.from_env({}),defaults)
        self.assertTrue(defaults.full_list_enabled)
        self.assertTrue(defaults.tail_gate_enabled)
        self.assertFalse(defaults.exact_entity_gate_enabled)
        self.assertFalse(defaults.graph_entity_name_only_enabled)
        configured=RecallSettings.from_env({'MEMORY_RERANK_EVIDENCE_MAX_CHILDREN':'invalid','MEMORY_RERANK_EVIDENCE_MAX_TOTAL_CHARS':'-10','MEMORY_RERANK_FULL_LIST_ENABLED':'false'})
        self.assertEqual(configured.evidence_max_children,defaults.evidence_max_children)
        self.assertEqual(configured.evidence_max_total_chars,0)
        self.assertFalse(configured.full_list_enabled)
        for kwargs in ({'full_list_enabled':'true'},{'evidence_max_children':True}):
            with self.assertRaises(ValueError):RecallSettings(**kwargs)

    def test_documented_defaults_match_program_and_example_has_no_overrides(self):
        root=Path(__file__).resolve().parents[1]
        readme=(root/'README.md').read_text()
        for key,value in RecallSettings.environment_defaults().items():
            literal=str(value).lower() if type(value) is bool else str(value)
            self.assertIn(f'| `{key}` | {literal} |',readme)
            self.assertNotIn(key+'=',(root/'.env.example').read_text())
