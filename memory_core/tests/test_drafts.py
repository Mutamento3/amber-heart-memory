from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier, Lock
from unittest.mock import patch
from uuid import uuid4

import psycopg

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.cards.drafts import DraftStore, context_for, structure_input
from memory_core.cards.extraction_contract import (ContractError, CredentialContent, check_credentials,
    json_object, material_input, render_prompt, selection_result, structure)
from memory_core.storage.install import install, catalog_fingerprint, MIGRATIONS
from memory_core.storage.space import Participant, RevisionConflict, SpaceConfiguration
from memory_core.testing.fixed_models import FixedModels
from memory_core.tests.support import DatabaseTestCase


class DraftTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        install(self.owner, Settings(768))
        self.config = SpaceConfiguration(self.owner)
        self.config.configure('example',Participant('Example User',('EU',)),Participant('Example Assistant'))
        self.store = DraftStore(self.app,'example')
        self.topic = self.store.fallback_topic()['id']

    def submit(self, key='one', *, stamp='2026-06-12T10:00:00+08:00', body='Example User plans to study botany.', day=None):
        return self.store.submit(key,'request:'+key,[dict(segment_id='s1',speaker='user',text=body,timestamp=stamp)],business_date=day)

    def candidate(self, *, score=8, suffix=''):
        return dict(title='Botany plan'+suffix,content='Example User plans to study botany.'+suffix,
                    summary='Example User plans to study botany.'+suffix,importance=score,
                    evidence=[dict(segment_id='s1',quote='plans to study botany.')])

    def graph(self):
        return dict(summary='Example User plans to study botany.',experience_mode='external',
            cognitive_status='stable',decay_layer='tier_4_episode',source_date='2026-06-12',is_milestone=False,
            valence=None,arousal=None,entities=[],relations=[],topic_ids=[self.topic])

    def models(self, extraction_id, candidates=None, graph=None):
        candidates = [self.candidate()] if candidates is None else candidates
        with connect(self.app) as connection:
            context = context_for(connection,'example')
        source_day = self.store.extraction(extraction_id)
        with connect(self.app) as connection:
            source = self.store._source(connection,source_day['material_id'])
            day = source['business_date']
        responses = {self.store.selection_prompt(extraction_id):json.dumps(dict(memories=candidates,skipped_reason=None if candidates else 'no valuable information'))}
        for candidate in candidates:
            responses[render_prompt('structure',context,structure_input(candidate,day,source['segments'],self.store.settings))] = json.dumps(self.graph() if graph is None else graph)
        return FixedModels({'extract':responses})

    def ready(self, key='one'):
        extraction = self.submit(key)
        models = self.models(extraction)
        result = self.store.extract(extraction,selector=models,structurer=models)
        return result['drafts'][0]

    def test_two_real_parsers_preserve_first_step_and_do_not_write_graph(self):
        extraction = self.submit()
        models = self.models(extraction)
        result = self.store.extract(extraction,selector=models,structurer=models)
        self.assertEqual(result['status'],'complete')
        draft = result['drafts'][0]
        self.assertEqual(draft['status'],'pending')
        self.assertEqual(draft['payload']['content'],self.candidate()['content'])
        self.assertEqual(draft['payload']['evidence'],self.candidate()['evidence'])
        self.assertEqual(len(models.calls),2)
        self.store.extract(extraction,selector=models,structurer=models)
        self.assertEqual(len(models.calls),2)
        with connect(self.app) as connection:
            self.assertEqual(connection.execute('SELECT count(*) AS n FROM memory_core.memory_items').fetchone()['n'],0)
            self.assertEqual(connection.execute('SELECT count(*) AS n FROM memory_core.entities').fetchone()['n'],2)

    def test_business_day_boundary_explicit_missing_time_and_cross_day(self):
        first = self.submit('before',stamp='2026-06-12T02:59:59+08:00')
        second = self.submit('after',stamp='2026-06-11T19:00:00+00:00')
        with connect(self.app) as connection:
            days = [r['business_date'] for r in connection.execute('SELECT business_date FROM memory_core.source_materials ORDER BY id')]
        self.assertEqual(days,['2026-06-11','2026-06-12'])
        self.submit('missing',stamp=None,day='2026-06-10')
        with self.assertRaisesRegex(ContractError,'missing_time'):
            self.submit('invalid',stamp=None)
        segments = [dict(segment_id=str(i),speaker='user',text='synthetic',timestamp=stamp) for i,stamp in enumerate(('2026-06-12T02:59:59+08:00','2026-06-12T03:00:00+08:00'))]
        with self.assertRaisesRegex(ContractError,'split_material'):
            material_input(segments,Settings())
        for stamp in ('2026-06-12T10:00:00','bad'):
            with self.assertRaises(ContractError): self.submit('bad-time',stamp=stamp)

    def test_importance_threshold_and_shared_daily_limit_do_not_refill_on_reject(self):
        extraction = self.submit()
        candidates = [self.candidate(score=6)]+[self.candidate(suffix=str(i)) for i in range(7)]
        model = self.models(extraction,candidates)
        result = self.store.extract(extraction,selector=model,structurer=model)
        self.assertEqual(len(result['drafts']),5)
        self.assertEqual(result['drafts'][0]['narrative']['importance'],8)
        draft = result['drafts'][0]
        self.store.reject(draft['id'],expected_version=draft['version'])
        other = self.submit('second-material')
        model = self.models(other)
        self.assertEqual(self.store.extract(other,selector=model,structurer=model)['skipped_reason'],'daily_limit_reached')
        self.assertEqual(len(self.store.list()),5)
        next_day = self.submit('next-day',stamp='2026-06-13T03:00:00+08:00')
        model = self.models(next_day)
        self.assertEqual(len(self.store.extract(next_day,selector=model,structurer=model)['drafts']),1)

    def test_space_settings_and_core_memory_are_authoritative(self):
        policy = self.config.set_extraction_rules('example',expected_revision=1,minimum_importance=9,daily_limit=2)
        policy = self.config.set_core_memory('example',expected_revision=2,core_memory='Example User already studies botany.')
        self.assertEqual(policy['revision'],3)
        self.assertEqual(policy['minimum_importance'],9)
        extraction = self.submit()
        self.assertIn('Example User already studies botany.',self.store.selection_prompt(extraction))
        model = self.models(extraction,[self.candidate(score=8),self.candidate(score=9,suffix='new progress')])
        result = self.store.extract(extraction,selector=model,structurer=model)
        self.assertEqual(len(result['drafts']),1)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with connect(self.app) as connection: connection.execute("UPDATE memory_core.space_policies SET daily_limit=99 WHERE bot_id='example'")
        for values in (dict(minimum_importance=True),dict(minimum_importance=0),dict(daily_limit=0)):
            with self.assertRaises(ValueError): self.config.set_extraction_rules('example',expected_revision=3,**values)

    def test_bad_selection_keeps_material_and_retries_without_duplicate_drafts(self):
        for index,raw in enumerate(('', '{"memories":', '[]','{"memories":[],"memories":[]}')):
            extraction = self.submit('bad-'+str(index))
            prompt = self.store.selection_prompt(extraction)
            broken = FixedModels({'extract':{prompt:raw}})
            result = self.store.extract(extraction,selector=broken,structurer=broken)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(len(broken.calls),2)
            model = self.models(extraction)
            repaired = self.store.extract(extraction,selector=model,structurer=model)
            self.assertEqual(len(repaired['drafts']),1)
            self.assertEqual(self.submit('bad-'+str(index)),extraction)
        with self.assertRaises(ValueError): self.submit('bad-0',body='different source')

    def test_structure_failure_keeps_narrative_evidence_and_retries_only_step_two(self):
        extraction = self.submit()
        graph = self.graph(); del graph['experience_mode']
        model = self.models(extraction,graph=graph)
        result = self.store.extract(extraction,selector=model,structurer=model)
        draft = result['drafts'][0]
        self.assertEqual(draft['status'],'structure_failed')
        self.assertEqual(draft['narrative']['evidence'],self.candidate()['evidence'])
        good = FixedModels({'extract':{self.store.structure_prompt(draft['id']):json.dumps(self.graph())}})
        repaired = self.store.extract(extraction,selector=None,structurer=good)
        self.assertEqual(repaired['drafts'][0]['status'],'pending')
        self.assertEqual(len(good.calls),1)

    def test_structures_cards_from_one_material_in_parallel_with_partial_failure(self):
        extraction = self.submit()
        candidates = [self.candidate(suffix=str(i)) for i in range(3)]
        selector = self.models(extraction, candidates)
        barrier = Barrier(3, timeout=8)
        calls = {}
        lock = Lock()
        bad_title = candidates[1]['title']
        success = json.dumps(self.graph())

        class Structurer:
            def extract(self, prompt):
                title = json.loads(prompt.rsplit('\n', 2)[1])['title']
                with lock:
                    calls[title] = calls.get(title, 0) + 1
                    first_call = calls[title] == 1
                if first_call:
                    barrier.wait()
                return '{}' if title == bad_title else success

        result = self.store.extract(extraction, selector=selector, structurer=Structurer())
        self.assertEqual(result['status'], 'selected')
        self.assertEqual([d['status'] for d in result['drafts']], ['pending', 'structure_failed', 'pending'])
        self.assertEqual(calls, {candidates[0]['title']: 1, bad_title: 2, candidates[2]['title']: 1})
        failed = result['drafts'][1]
        retry = FixedModels({'extract': {self.store.structure_prompt(failed['id']): success}})
        result = self.store.extract(extraction, selector=None, structurer=retry)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(retry.calls), 1)

    def test_late_structure_does_not_overwrite_an_edit(self):
        extraction = self.submit()
        broken = self.models(extraction, graph={})
        draft = self.store.extract(extraction, selector=broken, structurer=broken)['drafts'][0]
        target = self.store.structure_retry_target(draft['id'], expected_version=draft['version'])
        store = self.store
        manual = {**self.candidate(), **self.graph(), 'summary': '合成人工编辑。'}
        result = json.dumps(self.graph())

        class ModelWithConcurrentEdit:
            def extract(self, prompt):
                store.edit(draft['id'], manual, expected_version=draft['version'],
                           expected_policy_revision=draft['policy_revision'])
                return result

        with self.assertRaises(RevisionConflict):
            store.restructure(target, structurer=ModelWithConcurrentEdit())
        edited = store.get(draft['id'])
        self.assertEqual(edited['version'], draft['version'] + 1)
        self.assertEqual(edited['payload']['summary'], manual['summary'])
        self.assertIsNone(edited['structure_claim_token'])

    def test_expired_owner_cannot_write_or_clear_replacement_claim(self):
        extraction = self.submit()
        broken = self.models(extraction, graph={})
        draft = self.store.extract(extraction, selector=broken, structurer=broken)['drafts'][0]
        replacement = uuid4()
        store, dsn = self.store, self.app
        result = json.dumps(self.graph())

        class ModelWithExpiredClaim:
            def extract(self, prompt):
                with connect(dsn) as connection:
                    connection.execute("""UPDATE memory_core.pending_memories
                        SET structure_claim_until=memory_core.format_timestamp(clock_timestamp() - interval '1 second')
                        WHERE id=%s""", (draft['id'],))
                store._claim_structure(draft, replacement, 60)
                return result

        with self.assertRaises(RevisionConflict):
            store.restructure(draft, structurer=ModelWithExpiredClaim())
        store.structure_retry_failed(draft, 'structure:unknown')
        still_claimed = store.get(draft['id'])
        self.assertEqual(still_claimed['structure_claim_token'], replacement)
        self.assertEqual(still_claimed['version'], draft['version'])
        self.assertEqual(still_claimed['error_code'], draft['error_code'])
        shown = subprocess.run(
            [sys.executable, '-m', 'memory_core.daily.workflow', '--space', 'example', 'show', str(draft['id'])],
            env={'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1', 'MEMORY_DSN': self.app},
            capture_output=True, text=True,
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)['version'], draft['version'])

    def test_selection_preserves_quotes_and_drops_unknown_evidence_without_retry(self):
        extraction = self.submit(body='Example User plans to study botany.\nShe picked a class.')
        kept = dict(segment_id='s1', quote='plans to study botany. She picked a class.')
        unknown = dict(segment_id='missing', quote='She picked a class.')
        expected = [
            dict(self.candidate(), evidence=[kept]),
            dict(self.candidate(suffix=' without an anchor'), evidence=[]),
            dict(self.candidate(suffix=' with no evidence returned'), evidence=[]),
        ]
        candidates = [
            dict(expected[0], evidence=[kept, unknown]),
            dict(expected[1], evidence=[unknown, dict(segment_id='missing'), dict(quote='No segment ID.')]),
            expected[2],
        ]
        model = self.models(extraction, expected, graph=dict(self.graph(), source_date=None))
        selection = self.store.selection_prompt(extraction)
        model.fixtures['extract'][selection] = json.dumps(dict(memories=candidates, skipped_reason=None))
        result = self.store.extract(extraction, selector=model, structurer=model)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(model.calls), 4)
        for draft, candidate in zip(result['drafts'], expected, strict=True):
            self.assertEqual(draft['payload']['evidence'], candidate['evidence'])
            prompt = self.store.structure_prompt(draft['id'])
            material = json.loads(prompt.rsplit('\n', 2)[1])
            self.assertEqual([part['segment_id'] for part in material['evidence_times']],
                             [part['segment_id'] for part in candidate['evidence']])
            self.assertIsNone(draft['payload']['source_date'])

    def test_inputs_cannot_replace_placeholders_or_lose_whitespace(self):
        prompt = render_prompt('selection',{'core_memory':''},{'text':'{{SPACE_CONTEXT_JSON}}'})
        self.assertIn('"text": "{{SPACE_CONTEXT_JSON}}"',prompt)
        segments, _ = material_input([dict(segment_id='s1',speaker='user',text='  original text \n',timestamp='2026-06-12T10:00:00+08:00')],Settings())
        self.assertEqual(segments[0]['text'],'  original text \n')
        candidate = self.candidate()
        candidate['evidence'][0]['quote'] = ' plans to study botany. '
        result, _ = selection_result(json.dumps(dict(memories=[candidate], skipped_reason=None)), segments)
        self.assertEqual(result[0]['evidence'], candidate['evidence'])

    def test_edit_reject_version_and_policy_conflicts_and_cross_space(self):
        draft = self.ready()
        payload = deepcopy(draft['payload']); payload['importance']=0
        edited = self.store.edit(draft['id'],payload,expected_version=draft['version'],expected_policy_revision=1)
        self.assertEqual(edited['payload']['importance'],0)
        with self.assertRaises(RevisionConflict): self.store.reject(draft['id'],expected_version=draft['version'])
        self.config.set_core_memory('example',expected_revision=1,core_memory='Visible fact')
        with self.assertRaises(RevisionConflict): self.store.edit(draft['id'],payload,expected_version=edited['version'],expected_policy_revision=1)
        edited = self.store.edit(draft['id'],payload,expected_version=edited['version'],expected_policy_revision=2)
        self.assertEqual(edited['policy_revision'],2)
        self.store.reject(draft['id'],expected_version=edited['version'])
        self.config.configure('other',Participant('Other User'),Participant('Other Assistant'))
        other = DraftStore(self.app,'other')
        for action in (lambda:other.get(draft['id']),lambda:other.source(draft['id']),lambda:other.extraction(draft['extraction_id'])):
            with self.assertRaises(ValueError): action()
        self.assertEqual(other.list(),[])

    def test_source_tombstone_retention_and_no_reintroduction(self):
        draft = self.ready()
        source = self.store.source(draft['id'])
        self.assertEqual(self.store.expire_materials(now=datetime.fromisoformat('2030-01-01T00:00:00+08:00')),0)
        self.config.configure('example',Participant('Example User'),Participant('Example Assistant'),source_retention_days=1,expected_revision=1)
        self.assertEqual(self.store.expire_materials(now=datetime.fromisoformat('2030-01-01T00:00:00+08:00')),1)
        self.assertEqual(self.store.source(draft['id'])['source_status'],'原文已删除')
        self.assertIsNone(self.store.source(draft['id'])['segments'])
        self.assertIsNotNone(self.store.get(draft['id'])['payload'])
        with self.assertRaises(ValueError): self.submit()
        self.store.delete_material(source['id'])

    def test_concurrent_materials_share_budget_without_overflow(self):
        ids = [self.submit('parallel-'+str(i)) for i in range(2)]
        models = [self.models(e,[self.candidate(suffix=str(j)) for j in range(4)]) for e in ids]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda pair:self.store.extract(pair[0],selector=pair[1],structurer=pair[1]),zip(ids,models)))
        self.assertEqual(len(self.store.list()),5)

    def test_credential_like_text_is_not_screened_in_material_or_model_output(self):
        # Self-hosted: the text already went to the chat model and stays on the user's server.
        examples = ('password: synthetic-only-password', '验证码：123456', '银行卡号：4242424242424242', 'sk-'+('A'*25))
        for index,example in enumerate(examples):
            self.submit('credential-'+str(index),body=example)
        with connect(self.app) as connection:
            self.assertEqual(connection.execute('SELECT count(*) AS n FROM memory_core.source_materials').fetchone()['n'],len(examples))
        extraction = self.submit()
        candidate = self.candidate(); candidate['summary']='订单号 4242424242424242 已发货'
        model = self.models(extraction,[candidate])
        result = self.store.extract(extraction,selector=model,structurer=model)
        self.assertNotEqual(result['status'],'failed')

    def test_structural_semantics_topic_scope_and_relation_fallback(self):
        with connect(self.app) as connection: context = context_for(connection,'example')
        graph = self.graph()
        graph['topic_ids']=[999999]
        with self.assertRaises(ContractError): structure(graph,context)
        graph = self.graph(); graph['cognitive_status']='outdated'
        with self.assertRaises(ContractError): structure(graph,context)
        graph = self.graph()
        graph['entities']=[dict(ref='one',name='Object',known_entity_id=None,entity_type='CONCEPT',entity_subtype='artifact',aliases=[],mention_role='object',description='Synthetic object')]
        graph['relations']=[dict(source_ref='one',target_ref='one',relation_type='PARTICIPATES_IN',relation_type_raw=None,description='Synthetic relation',valid_from=None)]
        result = structure(graph,context)
        self.assertEqual(result['entities'][0]['entity_type'],'OBJECT')
        self.assertEqual(result['relations'][0]['relation_type'],'RELATES_TO')

    def test_default_cli_submit_extract_show_and_missing_fixture_error(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'source.json'
            source.write_text(json.dumps(dict(source_key='cli',request_key='cli',segments=[dict(segment_id='s1',speaker='user',text='Example User plans to study botany.',timestamp='2026-06-12T10:00:00+08:00')])))
            env={'PATH':os.environ['PATH'],'PYTHONDONTWRITEBYTECODE':'1','MEMORY_DSN':self.app}
            command=[sys.executable,'-m','memory_core.daily.workflow','--space','example']
            result=subprocess.run(command+['submit',str(source)],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            extraction=json.loads(result.stdout)['extraction_id']
            fixture=Path(folder)/'fixed.json'
            fixture.write_text(json.dumps(self.models(extraction).fixtures))
            result=subprocess.run(command+['extract',str(extraction),'--fixed-responses',str(fixture)],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            draft_id=json.loads(result.stdout)['draft_ids'][0]
            result=subprocess.run(command+['show',str(draft_id)],env=env,capture_output=True,text=True)
            self.assertEqual(json.loads(result.stdout)['status'],'pending')
            missing=Path(folder)/'missing.json'; missing.write_text('{}')
            another=self.submit('missing-fixture')
            result=subprocess.run(command+['extract',str(another),'--fixed-responses',str(missing)],env=env,capture_output=True,text=True)
            self.assertEqual(json.loads(result.stdout)['status'],'failed')

    def test_v3_upgrade_and_repeat_preserve_rules_materials_and_sequence(self):
        with connect(self.owner) as connection: connection.execute('DROP SCHEMA memory_core CASCADE')
        install(self.owner,Settings(768),target_version=3)
        self.config.configure('example',Participant('Example User'),Participant('Example Assistant'))
        install(self.owner,Settings(768))
        self.assertEqual(self.store.policy()['minimum_importance'],7)
        draft=self.ready()
        with connect(self.owner) as connection: fingerprint=catalog_fingerprint(connection)
        self.assertFalse(install(self.owner,Settings(768)))
        self.assertEqual(self.store.get(draft['id'])['payload']['content'],self.candidate()['content'])
        with connect(self.owner) as connection: self.assertEqual(catalog_fingerprint(connection),fingerprint)

    def test_v4_fresh_stepwise_catalog_and_migration_failure(self):
        with connect(self.owner) as connection: connection.execute('DROP SCHEMA memory_core CASCADE')
        install(self.owner,Settings(768),target_version=4)
        with connect(self.owner) as connection:
            expected=catalog_fingerprint(connection)
            connection.execute('DROP SCHEMA memory_core CASCADE')
        for version in (1,2,3): install(self.owner,Settings(768),target_version=version)
        with tempfile.TemporaryDirectory() as folder:
            for source in MIGRATIONS.glob('*.sql'):
                (Path(folder)/source.name).write_text(source.read_text()+('\nSELECT 1/0;' if source.name.startswith('004') else ''))
            with patch('memory_core.storage.install.MIGRATIONS',Path(folder)), self.assertRaises(psycopg.errors.DivisionByZero):
                install(self.owner,Settings(768),target_version=4)
        with connect(self.owner) as connection:
            self.assertEqual(connection.execute('SELECT version FROM memory_core.installation').fetchone()['version'],3)
            self.assertIsNone(connection.execute("SELECT to_regclass('memory_core.source_materials') AS v").fetchone()['v'])
        install(self.owner,Settings(768),target_version=4)
        with connect(self.owner) as connection: self.assertEqual(catalog_fingerprint(connection),expected)

    def test_explicit_reconfirm_after_configuration_change(self):
        extraction=self.submit()
        self.config.set_extraction_rules('example',expected_revision=1,minimum_importance=8)
        model=self.models(extraction)
        with self.assertRaises(RevisionConflict): self.store.extract(extraction,selector=model,structurer=model)
        with self.assertRaises(RevisionConflict): self.store.reconfirm_extraction(extraction,expected_version=1,expected_policy_revision=1)
        self.store.reconfirm_extraction(extraction,expected_version=1,expected_policy_revision=2)
        self.assertEqual(self.store.extract(extraction,selector=model,structurer=model)['status'],'complete')

    def test_second_step_gets_evidence_times_separate_from_business_day(self):
        extraction=self.submit(stamp='2026-06-13T01:00:00+08:00')
        graph=self.graph(); del graph['experience_mode']
        model=self.models(extraction,graph=graph)
        draft=self.store.extract(extraction,selector=model,structurer=model)['drafts'][0]
        prompt=self.store.structure_prompt(draft['id'])
        self.assertIn('"business_date": "2026-06-12"',prompt)
        self.assertIn('"timestamp": "2026-06-13T01:00:00+08:00"',prompt)
        self.assertIn('"timezone": "Asia/Shanghai"',prompt)
