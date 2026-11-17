"""Explicit material -> selection -> structure -> versioned review drafts."""

from datetime import datetime, timezone
import hashlib
import json
import re
from contextlib import nullcontext

from psycopg.types.json import Jsonb

from .db import connect
from .clock import format_timestamp
from .http_transport import ModelCallError
from .extraction_contract import (ContractError, CredentialContent, ENTITY_TYPES, ENTITY_SUBTYPES,
    RELATION_TYPES, check_credentials, identifier, material_input, narrative, render_prompt,
    selection_result, structure, json_object, text)
from .graph_store import GraphStore
from .schema import DRAFT_VERSION, TOPIC_DETAILS_VERSION, DELAYED_APPROVAL_VERSION, IMPORT_VERSION
from .space import RevisionConflict, read_policy, read_topics


def context_for(connection, space_id):
    policy = read_policy(connection, space_id)
    topics=[{key: topic[key] for key in ("id", "name", "is_fallback", "description")}
        for topic in read_topics(connection, space_id, policy=policy)]
    names={'用户':policy['participants']['user']['name'],'AI':policy['participants']['assistant']['name']}
    for topic in topics:
        # In topic descriptions these role words refer to this space's participants.
        # One pass leaves role-like substrings inside configured names unchanged.
        topic['description']=re.sub(r'用户|(?<![A-Za-z])\s*AI\s*(?![A-Za-z])',lambda match:names[match[0].strip()],topic['description'])
    return dict(policy_revision=policy['revision'], participants=policy['participants'],
        ignored_terms=policy['ignored_terms'], core_memory=policy.get('core_memory',''),
        minimum_importance=policy['minimum_importance'], daily_limit=policy['daily_limit'],
        known_entities=connection.execute('SELECT id,name,aliases,entity_type,entity_subtype FROM memory_core.entities WHERE bot_id=%s ORDER BY id', (space_id,)).fetchall(),
        topics=topics,
        entity_types=ENTITY_TYPES, entity_subtypes=ENTITY_SUBTYPES, relation_types=RELATION_TYPES)


def context_digest(context):
    # Entity growth from earlier approved cards is expected during a retry.
    stable = {
        k: v
        for k, v in context.items()
        if k not in ("policy_revision", "known_entities")
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def capture_policy(connection, space_id, *, draft_id=None, extraction_id=None):
    version=connection.execute('SELECT version FROM memory_core.installation').fetchone()['version']
    if version < TOPIC_DETAILS_VERSION:
        return
    if draft_id is not None:
        connection.execute('UPDATE memory_core.pending_memories SET policy_snapshot=memory_core.configuration_snapshot(%s) WHERE bot_id=%s AND id=%s', (space_id,space_id,draft_id))
    else:
        connection.execute('UPDATE memory_core.pending_memories SET policy_snapshot=memory_core.configuration_snapshot(%s) WHERE bot_id=%s AND extraction_id=%s', (space_id,space_id,extraction_id))


def structure_input(first, day, segments, settings):
    ids={e['segment_id'] for e in first['evidence']}
    return dict(**first,business_date=day,timezone=settings.timezone,day_start_hour=settings.day_start_hour,
                evidence_times=[{k:s[k] for k in ('segment_id','timestamp','speaker')} for s in segments if s['segment_id'] in ids])


class MaterialExpired(ValueError):
    pass


class ExtractionConfigurationChanged(RevisionConflict):
    pass


class StructureInProgress(RevisionConflict):
    pass


def structure_error(error):
    if isinstance(error, CredentialContent):
        return 'structure:credentials'
    if isinstance(error, TimeoutError) or isinstance(error, ModelCallError) and error.code.endswith('_timeout'):
        return 'structure:timeout'
    if isinstance(error, ModelCallError):
        return 'structure:rate_limited' if error.code.endswith('_http_429') else 'structure:provider'
    if isinstance(error, ContractError):
        return 'structure:format'
    return 'structure:unknown'


class DraftStore(GraphStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        with connect(dsn) as connection:
            if connection.execute('SELECT version FROM memory_core.installation').fetchone()['version'] < DRAFT_VERSION:
                raise ValueError('请先升级到材料/草稿支持的版本')

    def _extraction(self, connection, extraction_id, *, lock=False):
        identifier(extraction_id, 'extraction_id')
        row = connection.execute('SELECT * FROM memory_core.material_extractions WHERE id=%s AND bot_id=%s' + (' FOR UPDATE' if lock else ''), (extraction_id, self.space_id)).fetchone()
        if not row:
            raise ValueError('抽取请求不存在或属于其他空间')
        return row

    def _draft(self, connection, draft_id, *, lock=False):
        identifier(draft_id, 'draft_id')
        row = connection.execute('SELECT * FROM memory_core.pending_memories WHERE id=%s AND bot_id=%s' + (' FOR UPDATE' if lock else ''), (draft_id, self.space_id)).fetchone()
        if not row:
            raise ValueError('草稿不存在或属于其他空间')
        return row

    def _source(self, connection, material_id, *, lock=False):
        return connection.execute('SELECT * FROM memory_core.source_materials WHERE id=%s AND bot_id=%s' + (' FOR UPDATE' if lock else ''), (material_id, self.space_id)).fetchone()

    def submit(
        self,
        source_key,
        request_key,
        segments,
        *,
        source_kind="mainline",
        business_date=None,
        message_business_dates=None,
        material_kind='chat',
        connection=None
    ):
        source_key, request_key = text(source_key, 'source_key'), text(request_key, 'request_key')
        if source_kind != "mainline":
            raise ValueError('材料来源无效')
        if material_kind not in ('chat','memory') or (material_kind=='memory' and self.schema_version<IMPORT_VERSION):
            raise ValueError('材料类型无效或需要先升级到导入版本')
        segments, day = material_input(
            segments,
            self.settings,
            business_date,
            message_business_dates=message_business_dates,
        )
        check_credentials([source_key, request_key])
        digest = hashlib.sha256(json.dumps([day,source_kind,segments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with (
            nullcontext(connection) if connection is not None else connect(self.dsn)
        ) as connection:
            policy = read_policy(connection, self.space_id)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('source:'+self.space_id+':'+source_key,))
            material = connection.execute('SELECT * FROM memory_core.source_materials WHERE bot_id=%s AND source_key=%s FOR UPDATE', (self.space_id,source_key)).fetchone()
            if material:
                if material['content_sha256'] != digest or material.get('material_kind','chat') != material_kind:
                    raise ValueError('来源标识已用于不同材料，不能覆盖原文')
                if material['deleted_at']:
                    raise ValueError('原文已删除，不重新保存或抽取')
                existing = connection.execute('SELECT * FROM memory_core.material_extractions WHERE bot_id=%s AND material_id=%s', (self.space_id,material['id'])).fetchone()
                if existing['request_key'] != request_key:
                    raise ValueError('同一来源已有抽取请求，请使用原请求标识重试')
                return existing['id']
            material = connection.execute('''INSERT INTO memory_core.source_materials
                (bot_id,source_key,content_sha256,business_date,source_kind,segments)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''', (self.space_id,source_key,digest,day,source_kind,Jsonb(segments))).fetchone()
            if material_kind == 'memory':
                connection.execute("UPDATE memory_core.source_materials SET material_kind='memory' WHERE id=%s AND bot_id=%s", (material['id'],self.space_id))
            return connection.execute('''INSERT INTO memory_core.material_extractions(bot_id,material_id,request_key,policy_revision)
                VALUES (%s,%s,%s,%s) RETURNING id''', (self.space_id,material['id'],request_key,policy['revision'])).fetchone()['id']

    def submit_memory(self, source_key, request_key, content, source_date, *, connection=None):
        """User-selected prose bypasses selection, scoring and chat daily quota."""
        with (nullcontext(connection) if connection is not None else connect(self.dsn)) as c:
            segments=[dict(segment_id='m-0',speaker='user',text=content,timestamp=None)]
            extraction_id=self.submit(source_key,request_key,segments,business_date=source_date,
                material_kind='memory',connection=c)
            extraction=self._extraction(c,extraction_id,lock=True)
            if extraction['status']=='pending':
                first=narrative(dict(title='待整理的记忆',content=content,summary=content,importance=10,
                    evidence=[dict(segment_id='m-0',quote=content)]),segments)
                c.execute('''INSERT INTO memory_core.pending_memories
                    (bot_id,extraction_id,ordinal,business_date,policy_revision,narrative)
                    VALUES (%s,%s,0,%s,%s,%s)''',
                    (self.space_id,extraction_id,source_date,extraction['policy_revision'],Jsonb(first)))
                capture_policy(c,self.space_id,extraction_id=extraction_id)
                c.execute("UPDATE memory_core.material_extractions SET status='selected',version=version+1 WHERE id=%s AND bot_id=%s",(extraction_id,self.space_id))
            return extraction_id

    def selection_prompt(self, extraction_id):
        with connect(self.dsn) as connection:
            context = context_for(connection, self.space_id)
            extraction = self._extraction(connection, extraction_id)
            source = self._source(connection, extraction['material_id'])
            if source['deleted_at']:
                raise MaterialExpired('原文已删除')
            return render_prompt('selection', context, dict(business_date=source['business_date'], timezone=self.settings.timezone,
                day_start_hour=self.settings.day_start_hour, segments=source['segments']))

    def structure_prompt(self, draft_id):
        with connect(self.dsn) as connection:
            context = context_for(connection, self.space_id)
            draft = self._draft(connection, draft_id)
            if context['policy_revision'] != draft['policy_revision']:
                raise ExtractionConfigurationChanged('配置已改变，请先复核草稿')
            extraction=self._extraction(connection,draft['extraction_id'])
            source=self._source(connection,extraction['material_id'])
            if source['deleted_at']:
                raise MaterialExpired('原文已删除')
            if source.get('material_kind')=='memory':
                return render_prompt('memory_structure',context,
                    dict(source_date=source['business_date'],content=source['segments'][0]['text']))
            return render_prompt('structure', context, structure_input(draft['narrative'],draft['business_date'],source['segments'],self.settings))

    @staticmethod
    def _model_call(model, prompt, parse, *, retry_rate_limit=False):
        if model is None:
            raise ValueError('必须显式提供模型岗位；不会回退到真实模型')
        for attempt in range(2):
            try:
                return parse(model.extract(prompt))
            except CredentialContent:
                raise
            except (ContractError, TimeoutError):
                if attempt == 1:
                    raise
            except ModelCallError as error:
                if attempt == 1 or not (error.code.endswith('_timeout')
                    or (retry_rate_limit and error.code.endswith('_http_429'))):
                    raise

    def extract(self, extraction_id, *, selector, structurer, clock=None, retry_rate_limit=False):
        with connect(self.dsn) as connection:
            context = context_for(connection, self.space_id)
            extraction = self._extraction(connection, extraction_id)
            source = self._source(connection, extraction['material_id'])
        if extraction['status']=='complete':
            return self.extraction(extraction_id)
        if source['deleted_at']:
            raise MaterialExpired('原文已删除')
        if extraction['policy_revision'] != context['policy_revision']:
            raise ExtractionConfigurationChanged('配置已改变，需要明确复核后重试')
        if extraction['status'] in ('pending','failed'):
            try:
                candidates, reason = self._model_call(selector, self.selection_prompt(extraction_id),
                    lambda raw: selection_result(raw,source['segments']), retry_rate_limit=retry_rate_limit)
            except (MaterialExpired, RevisionConflict):
                raise
            except Exception as exc:
                self._selection_error(extraction, exc)
                return self.extraction(extraction_id)
            with connect(self.dsn) as connection:
                current = context_for(connection, self.space_id)
                if current['policy_revision'] != context['policy_revision']:
                    raise ExtractionConfigurationChanged('模型返回时配置已改变')
                # Serialize the shared daily budget; model calls happen outside it.
                connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('quota:'+self.space_id+':'+source['business_date'],))
                original = self._source(connection, source['id'], lock=True)
                if original['deleted_at']:
                    raise MaterialExpired('原文已删除')
                actual = self._extraction(connection, extraction_id, lock=True)
                if actual['version'] != extraction['version']:
                    raise RevisionConflict('抽取请求已被其他任务处理，请重新读取')
                used = self.daily_candidate_count(connection, source['business_date'])
                available = max(0,current['daily_limit']-used)
                selected = [c for c in candidates if c['importance']>=current['minimum_importance']][:available]
                for ordinal, candidate in enumerate(selected):
                    connection.execute('''INSERT INTO memory_core.pending_memories
                        (bot_id,extraction_id,ordinal,business_date,policy_revision,narrative)
                        VALUES (%s,%s,%s,%s,%s,%s)''', (self.space_id,extraction_id,ordinal,source['business_date'],current['policy_revision'],Jsonb(candidate)))
                capture_policy(connection,self.space_id,extraction_id=extraction_id)
                reason = None if selected else (reason or ('daily_limit_reached' if not available else 'below_importance_threshold'))
                connection.execute('''UPDATE memory_core.material_extractions SET status=%s,version=version+1,error_code=NULL,skipped_reason=%s,
                    updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE id=%s AND bot_id=%s''', ('selected' if selected else 'complete',reason,extraction_id,self.space_id))
        for draft in self.extraction(extraction_id)['drafts']:
            if draft['status'] in ('structure_pending','structure_failed'):
                self._structure(draft, structurer, clock=clock, retry_rate_limit=retry_rate_limit)
        self._settle_structure(extraction_id)
        return self.extraction(extraction_id)

    def _settle_structure(self, extraction_id):
        with connect(self.dsn) as connection:
            self._extraction(connection, extraction_id, lock=True)
            unfinished = connection.execute("SELECT EXISTS(SELECT 1 FROM memory_core.pending_memories WHERE bot_id=%s AND extraction_id=%s AND status IN ('structure_pending','structure_failed')) AS value", (self.space_id,extraction_id)).fetchone()['value']
            connection.execute('''UPDATE memory_core.material_extractions SET status=%s,version=version+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE id=%s AND bot_id=%s''', ('selected' if unfinished else 'complete',extraction_id,self.space_id))

    def structure_retry_target(self, draft_id, *, expected_version):
        with connect(self.dsn) as connection:
            draft = self._draft(connection, draft_id)
            if draft['version'] != expected_version or draft['status'] != 'structure_failed':
                raise RevisionConflict('草稿已改变或不需要重新整理')
            if read_policy(connection, self.space_id)['revision'] != draft['policy_revision']:
                raise ExtractionConfigurationChanged('配置已改变，请先复核草稿')
            extraction = self._extraction(connection, draft['extraction_id'])
            if self._source(connection, extraction['material_id'])['deleted_at']:
                raise MaterialExpired('原文已删除')
            return draft

    def restructure(self, draft, *, structurer, clock=None):
        self._structure(draft, structurer, clock=clock)
        self._settle_structure(draft['extraction_id'])
        return self.get(draft['id'])

    def daily_candidate_count(self, connection, day):
        return self.daily_candidate_counts(connection,[day]).get(day,0)

    def daily_candidate_counts(self, connection, days):
        if self.schema_version < IMPORT_VERSION:
            rows=connection.execute('SELECT business_date,count(*) AS n FROM memory_core.pending_memories WHERE bot_id=%s AND business_date=ANY(%s) GROUP BY business_date', (self.space_id,days)).fetchall()
            return {row['business_date']:row['n'] for row in rows}
        rows=connection.execute('''SELECT p.business_date,count(*) AS n FROM memory_core.pending_memories p
            JOIN memory_core.material_extractions e ON (e.id,e.bot_id)=(p.extraction_id,p.bot_id)
            JOIN memory_core.source_materials s ON (s.id,s.bot_id)=(e.material_id,e.bot_id)
            WHERE p.bot_id=%s AND p.business_date=ANY(%s) AND s.material_kind='chat' GROUP BY p.business_date''',
            (self.space_id,days)).fetchall()
        return {row['business_date']:row['n'] for row in rows}

    def _selection_error(self, previous, error):
        with connect(self.dsn) as connection:
            connection.execute('''UPDATE memory_core.material_extractions SET status='failed',error_code=%s,version=version+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE id=%s AND bot_id=%s AND version=%s''',
                ('selection:'+type(error).__name__,previous['id'],self.space_id,previous['version']))

    def _structure(self, draft, model, *, clock=None, retry_rate_limit=False):
        # The session lock covers the model call without holding a database
        # transaction open. A restart releases it and leaves the draft intact.
        with connect(self.dsn) as guard:
            locked = guard.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked',
                (f'draft-structure:{self.space_id}:{draft["id"]}',)).fetchone()['locked']
            guard.commit()
            if not locked:
                raise StructureInProgress('这张草稿正在整理')
            current = self._draft(guard, draft['id'])
            guard.commit()
            if current['version'] != draft['version']:
                raise RevisionConflict('草稿已被其他任务处理，请重新读取')
            return self._run_structure(draft, model, clock=clock, retry_rate_limit=retry_rate_limit)

    def _run_structure(self, draft, model, *, clock=None, retry_rate_limit=False):
        with connect(self.dsn) as connection:
            context = context_for(connection, self.space_id)
            extraction = self._extraction(connection,draft['extraction_id'])
            source = self._source(connection,extraction['material_id'])
        if context['policy_revision'] != draft['policy_revision']:
            raise ExtractionConfigurationChanged('配置已改变，请先复核草稿')
        try:
            def parse(raw):
                value=json_object(raw)
                if source.get('material_kind')=='memory':
                    title=text(value.pop('title',None),'title')
                    if 'content' not in value:
                        raise ContractError('memory_content_decision_required')
                    rewritten=value.pop('content')
                    content=source['segments'][0]['text'] if rewritten is None else text(rewritten,'content')
                    check_credentials([title,content])
                    result=structure(value,context)
                    if result['source_date'] != source['business_date']:
                        raise ContractError('memory_source_date_changed')
                    return dict(result,title=title,content=content)
                return structure(value,context)
            result = self._model_call(model, self.structure_prompt(draft['id']), parse,
                retry_rate_limit=retry_rate_limit)
            payload, status, error = {**draft['narrative'], **result}, 'pending', None
        except (MaterialExpired, RevisionConflict):
            raise
        except Exception as exc:
            payload, status, error = None, 'structure_failed', structure_error(exc)
        with connect(self.dsn) as connection:
            current = context_for(connection, self.space_id)
            if current['policy_revision'] != context['policy_revision']:
                raise ExtractionConfigurationChanged('模型返回时配置已改变')
            extraction = self._extraction(connection,draft['extraction_id'])
            source = self._source(connection,extraction['material_id'],lock=True)
            if source['deleted_at']:
                raise MaterialExpired('原文已删除')
            ready_clause=''
            first=dict(draft['narrative'])
            if payload is not None and source.get('material_kind')=='memory':
                first['title']=payload['title']
            values=[Jsonb(first),Jsonb(payload) if payload is not None else None,status,error]
            if self.schema_version>=DELAYED_APPROVAL_VERSION:
                timestamp=format_timestamp(clock() if clock else datetime.now(timezone.utc),self.settings)
                ready_clause=",review_ready_at=CASE WHEN %s='pending' THEN coalesce(review_ready_at,%s) ELSE review_ready_at END"
                values.extend([status,timestamp])
            values.extend([draft['id'],self.space_id,draft['version']])
            updated = connection.execute('''UPDATE memory_core.pending_memories SET narrative=%s,payload=%s,status=%s,error_code=%s,version=version+1'''+ready_clause+''',
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE id=%s AND bot_id=%s AND version=%s RETURNING id''',values).fetchone()
            if not updated:
                raise RevisionConflict('草稿已被其他任务处理，请重新读取')

    def structure_retry_failed(self, draft, error):
        with connect(self.dsn) as connection:
            connection.execute('''UPDATE memory_core.pending_memories SET error_code=%s,version=version+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP)
                WHERE bot_id=%s AND id=%s AND version=%s AND status='structure_failed' ''',
                (error,self.space_id,draft['id'],draft['version']))

    def extraction(self, extraction_id):
        with connect(self.dsn) as connection:
            row = self._extraction(connection, extraction_id)
            drafts = connection.execute('SELECT * FROM memory_core.pending_memories WHERE bot_id=%s AND extraction_id=%s ORDER BY ordinal', (self.space_id,extraction_id)).fetchall()
            return {**row, 'drafts':drafts}

    def get(self, draft_id):
        with connect(self.dsn) as connection:
            return self._draft(connection,draft_id)

    def list(self, *, after_id=0, limit=50):
        if type(after_id) is not int or after_id<0 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('分页参数无效')
        with connect(self.dsn) as connection:
            return connection.execute('SELECT * FROM memory_core.pending_memories WHERE bot_id=%s AND id>%s ORDER BY id LIMIT %s', (self.space_id,after_id,limit)).fetchall()

    def edit(self, draft_id, payload, *, expected_version, expected_policy_revision, clock=None):
        identifier(expected_version,'expected_version')
        identifier(expected_policy_revision,'expected_policy_revision')
        with connect(self.dsn) as connection:
            context = context_for(connection,self.space_id)
            if context['policy_revision'] != expected_policy_revision:
                raise RevisionConflict('配置已改变，请重新确认')
            draft = self._draft(connection,draft_id)
            extraction = self._extraction(connection,draft['extraction_id'])
            source = self._source(connection,extraction['material_id'],lock=True)
            draft = self._draft(connection,draft_id,lock=True)
            if draft['version'] != expected_version or draft['status'] not in ('pending','structure_pending','structure_failed'):
                raise RevisionConflict('草稿已改变或已完成审核')
            if source['deleted_at']:
                raise ValueError('原文已删除，不能重新编辑收录')
            if not isinstance(payload,dict):
                raise ContractError('payload_object_required')
            narrative_fields = ('title','content','summary','importance','evidence')
            # summary belongs to both contracts, with the final reviewed value.
            first = narrative({k:payload.get(k) for k in narrative_fields},source['segments'],edited=True)
            second = structure({k:v for k,v in payload.items() if k not in ('title','content','importance','evidence')},context)
            revised = {**first,**second}
            ready_clause=''
            values=[Jsonb(first),Jsonb(revised),context['policy_revision']]
            if self.schema_version>=DELAYED_APPROVAL_VERSION:
                ready_clause=',review_ready_at=%s'
                values.append(format_timestamp(clock() if clock else datetime.now(timezone.utc),self.settings))
            values.extend([draft_id,self.space_id])
            connection.execute('''UPDATE memory_core.pending_memories SET narrative=%s,payload=%s,status='pending',error_code=NULL,
                policy_revision=%s,version=version+1,updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP)'''+ready_clause+'''
                WHERE id=%s AND bot_id=%s''',values)
            capture_policy(connection,self.space_id,draft_id=draft_id)
            return self._draft(connection,draft_id)

    def reject(self, draft_id, *, expected_version, connection=None):
        identifier(expected_version,'expected_version')
        with (
            nullcontext(connection) if connection is not None else connect(self.dsn)
        ) as connection:
            draft = self._draft(connection,draft_id,lock=True)
            if draft['version'] != expected_version or draft['status'] not in ('pending','structure_pending','structure_failed'):
                raise RevisionConflict('草稿已改变或已完成审核')
            connection.execute("UPDATE memory_core.pending_memories SET status='rejected',version=version+1,updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE id=%s AND bot_id=%s", (draft_id,self.space_id))
            return self._draft(connection,draft_id)

    def reconfirm_extraction(
        self,
        extraction_id,
        *,
        expected_version,
        expected_policy_revision,
        unchanged_context_digest=None
    ):
        """Explicit owner review of changed configuration before selection retry."""
        identifier(expected_version,'expected_version')
        identifier(expected_policy_revision,'expected_policy_revision')
        with connect(self.dsn) as connection:
            policy = read_policy(connection,self.space_id)
            if policy['revision'] != expected_policy_revision:
                raise RevisionConflict('配置已改变')
            extraction = self._extraction(connection,extraction_id,lock=True)
            allow_selected = (
                unchanged_context_digest is not None
                and unchanged_context_digest
                == context_digest(context_for(connection, self.space_id))
            )
            if extraction["version"] != expected_version or (
                extraction["status"] not in ("pending", "failed") and not allow_selected
            ):
                raise RevisionConflict('请求已处理；已有候选须逐条复核')
            if allow_selected:
                updated = connection.execute(
                    """UPDATE memory_core.pending_memories SET policy_revision=%s,version=version+1,
                    updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s AND extraction_id=%s
                    AND status IN ('pending','structure_pending','structure_failed') RETURNING id""",
                    (policy["revision"], self.space_id, extraction_id),
                ).fetchall()
                for draft in updated:
                    capture_policy(connection, self.space_id, draft_id=draft["id"])
            connection.execute('UPDATE memory_core.material_extractions SET policy_revision=%s,version=version+1 WHERE id=%s AND bot_id=%s',
                               (policy['revision'],extraction_id,self.space_id))
            return {**extraction,'policy_revision':policy['revision'],'version':extraction['version']+1}

    def source(self, draft_id):
        with connect(self.dsn) as connection:
            draft = self._draft(connection,draft_id)
            if draft['status']=='deleted':
                raise ValueError('对应卡片已删除，审核记录只保留计数')
            extraction = self._extraction(connection,draft['extraction_id'])
            source = self._source(connection,extraction['material_id'])
            return {**source,'source_status':'原文已删除' if source['deleted_at'] else 'available'}

    def delete_material(self, material_id):
        identifier(material_id,'material_id')
        with connect(self.dsn) as connection:
            row = self._source(connection,material_id,lock=True)
            if row is None:
                raise ValueError('材料不存在或属于其他空间')
            connection.execute('''UPDATE memory_core.source_materials SET segments=NULL,
                deleted_at=coalesce(deleted_at,memory_core.format_timestamp(CURRENT_TIMESTAMP)) WHERE id=%s AND bot_id=%s''', (material_id,self.space_id))

    def expire_materials(self, *, now):
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('清理时间必须带偏移')
        with connect(self.dsn) as connection:
            policy = read_policy(connection,self.space_id)
            days = policy['source_retention_days']
            if days is None:
                return 0
            return connection.execute('''UPDATE memory_core.source_materials SET segments=NULL,
                deleted_at=memory_core.format_timestamp(%s::timestamptz) WHERE bot_id=%s AND deleted_at IS NULL
                AND created_at::timestamptz<=%s::timestamptz-make_interval(days=>%s)''', (now,self.space_id,now,days)).rowcount
