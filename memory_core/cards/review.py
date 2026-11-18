"""Prepare vectors before writes; commit the full reviewed graph atomically."""

from copy import deepcopy
import hashlib
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from ..storage.db import connect
from .drafts import DraftStore, context_for
from .extraction_contract import check_credentials, identifier, narrative, structure, text
from .graph_store import AmbiguousEntity
from .keyword_index import build_rows
from ..models.models import embed_for
from ..storage.schema import REVIEW_VERSION
from ..storage.space import RevisionConflict


class PreparationError(ValueError):
    pass


def entity_embedding_text(entity):
    parts=['实体：'+entity['name']]
    aliases=list(dict.fromkeys(entity['aliases']))
    if aliases:
        parts.append('别名：'+'、'.join(aliases))
    if entity.get('description'):
        parts.append('描述：'+entity['description'])
    return '\n'.join(parts)


class ReviewStore(DraftStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn,space_id)
        with connect(dsn) as connection:
            if connection.execute('SELECT version FROM memory_core.installation').fetchone()['version']<REVIEW_VERSION:
                raise ValueError('请先升级到审核事务支持的版本')

    @staticmethod
    def _checkpoint(stage, connection):
        """Fault-injection seam; no production side effects or callbacks by default."""

    def _resolve(self, connection, entity):
        eid=entity['known_entity_id']
        if eid is not None:
            row=connection.execute('SELECT * FROM memory_core.entities WHERE bot_id=%s AND id=%s',(self.space_id,eid)).fetchone()
            if row is None:
                raise ValueError('实体不存在或属于其他空间')
            return row
        matches=connection.execute('''SELECT * FROM memory_core.entities WHERE bot_id=%s
            AND lookup_keys && memory_core.entity_lookup_keys(%s,%s) ORDER BY id''',
            (self.space_id,entity['name'],entity['aliases'])).fetchall()
        if len(matches)>1:
            raise AmbiguousEntity('同名或别名命中多个实体，请明确实体引用')
        return matches[0] if matches else None

    @staticmethod
    def _snapshot(entity):
        return {k:entity[k] for k in ('id','name','aliases','entity_type','entity_subtype','description','embedding_text_hash','embedding_model')}

    def _prepare_entities(self, payload, context, embedder, model_name):
        plans,refs={},{}
        participant_ids={p['id'] for p in context['participants'].values()}
        # Resolve first, close read transaction, then call supplied models.
        with connect(self.dsn) as connection:
            for entity in payload['entities']:
                existing=self._resolve(connection,entity)
                normalized=connection.execute('SELECT memory_core.normalize_entity_name(%s) AS name',(entity['name'],)).fetchone()['name']
                key=('existing',existing['id']) if existing else ('new',normalized)
                refs[entity['ref']]=key
                if key in plans:
                    continue
                merged=deepcopy(entity)
                if existing:
                    merged.update(name=existing['name'],entity_type=existing['entity_type'],
                                  entity_subtype=existing['entity_subtype'] or (entity['entity_subtype'] if existing['entity_type']==entity['entity_type'] else None),
                                  aliases=list(dict.fromkeys(existing['aliases']+entity['aliases'])),
                                  description=entity['description'] or existing['description'])
                protected=existing is not None and existing['id'] in participant_ids
                if protected:
                    merged=existing
                plans[key]=dict(incoming=entity,entity=merged,snapshot=self._snapshot(existing) if existing else None,
                                protected=protected,entity_key=existing['entity_key'] if existing else 'review:'+uuid4().hex)
        for plan in plans.values():
            if plan['protected']:
                continue
            content=entity_embedding_text(plan['entity'])
            try:
                plan['vector']=self._vector(embed_for(
                    embedder, content, purpose="document", title=plan['entity']['name']
                ))
            except Exception as exc:
                raise PreparationError('entity_embedding:'+type(exc).__name__) from None
            plan['embedding_text_hash']=hashlib.sha256(content.encode()).hexdigest()
            plan['embedding_model']=model_name
        return plans,refs

    def approve(
        self,
        draft_id,
        *,
        expected_version,
        expected_policy_revision,
        request_key,
        embedder,
        embedding_model,
        guard=None
    ):
        identifier(expected_version,'expected_version')
        identifier(expected_policy_revision,'expected_policy_revision')
        request_key=text(request_key,'review_request_key')
        model_name=text(embedding_model,'embedding_model')
        check_credentials([request_key,model_name])
        with connect(self.dsn) as connection:
            context=context_for(connection,self.space_id)
            if guard is not None:
                guard(connection)
            draft=self._draft(connection,draft_id)
            collision=connection.execute('SELECT id FROM memory_core.pending_memories WHERE bot_id=%s AND review_key=%s',(self.space_id,request_key)).fetchone()
            if collision and collision['id']!=draft_id:
                raise ValueError('审核请求标识已经用于其他草稿')
            if draft['status']=='approved' or draft['review_key'] is not None:
                return self._result(draft)
            if draft['version']!=expected_version or draft['status']!='pending':
                raise RevisionConflict('草稿已改变或不在待审状态')
            if draft['policy_revision']!=expected_policy_revision or context['policy_revision']!=expected_policy_revision:
                raise RevisionConflict('配置已改变，请先复核草稿')
            extraction=self._extraction(connection,draft['extraction_id'])
            source=self._source(connection,extraction['material_id'])
            if source['deleted_at']:
                raise ValueError('原文已删除，不能首次确认收录')
            first=narrative({k:draft['payload'][k] for k in ('title','content','summary','importance','evidence')},
                source['segments'],edited=True)
            second=structure({k:v for k,v in draft['payload'].items() if k not in ('title','content','importance','evidence')},context)
            payload={**first,**second}
        if embedder is None:
            raise ValueError('必须显式提供嵌入岗位')
        try:
            memory_vector=self._vector(embed_for(
                embedder, payload['summary'], purpose="document", title=payload['title']
            ))
        except Exception as exc:
            raise PreparationError('memory_embedding:'+type(exc).__name__) from None
        plans,refs=self._prepare_entities(payload,context,embedder,model_name)
        with connect(self.dsn) as connection:
            current=context_for(connection,self.space_id)
            if guard is not None:
                guard(connection)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('review:'+self.space_id,))
            current_source=self._source(connection,source['id'],lock=True)
            actual=self._draft(connection,draft_id,lock=True)
            if actual['status']=='approved' or actual['review_key'] is not None:
                return self._result(actual)
            if actual['version']!=expected_version or actual['status']!='pending' or actual['policy_revision']!=expected_policy_revision or current['policy_revision']!=expected_policy_revision:
                raise RevisionConflict('审核期间草稿或配置已改变，请重新确认')
            if current_source['deleted_at']:
                raise ValueError('审核期间原文已删除')
            # Revalidate every mutable entity used for vector preparation.
            for plan in plans.values():
                existing=self._resolve(connection,plan['incoming'])
                if (self._snapshot(existing) if existing else None)!=plan['snapshot']:
                    raise RevisionConflict('实体在准备向量期间已改变，请重试')
                if existing:
                    connection.execute('SELECT id FROM memory_core.entities WHERE bot_id=%s AND id=%s FOR UPDATE',(self.space_id,existing['id']))
                    locked=connection.execute('SELECT * FROM memory_core.entities WHERE bot_id=%s AND id=%s',(self.space_id,existing['id'])).fetchone()
                    if self._snapshot(locked)!=plan['snapshot']:
                        raise RevisionConflict('实体并发修改，请重试')
            card=connection.execute('''INSERT INTO memory_core.memory_items
                (bot_id,title,content,summary,embedding,source_date,is_milestone,decay_layer,cognitive_status,
                 valence,arousal,source_kind,experience_mode)
                VALUES (%s,%s,%s,%s,%s::public.vector,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (self.space_id,payload['title'],payload['content'],payload['summary'],memory_vector,payload['source_date'],
                 payload['is_milestone'],payload['decay_layer'],payload['cognitive_status'],payload['valence'],payload['arousal'],source['source_kind'],payload['experience_mode'])).fetchone()
            self._checkpoint('card',connection)
            entities={}
            for key,plan in plans.items():
                entity=plan['entity']
                if plan['protected']:
                    saved=entity
                elif plan['snapshot'] is None:
                    saved=connection.execute('''INSERT INTO memory_core.entities
                        (bot_id,entity_key,name,aliases,entity_type,entity_subtype,description,embedding,embedding_text_hash,embedding_model)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::public.vector,%s,%s) RETURNING *''',
                        (self.space_id,plan['entity_key'],entity['name'],entity['aliases'],entity['entity_type'],entity['entity_subtype'],entity['description'],plan['vector'],plan['embedding_text_hash'],model_name)).fetchone()
                else:
                    saved=connection.execute('''UPDATE memory_core.entities SET aliases=%s,entity_subtype=%s,description=%s,
                        embedding=%s::public.vector,embedding_text_hash=%s,embedding_model=%s,
                        updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s AND id=%s RETURNING *''',
                        (entity['aliases'],entity['entity_subtype'],entity['description'],plan['vector'],plan['embedding_text_hash'],model_name,self.space_id,plan['snapshot']['id'])).fetchone()
                entities[key]=saved
            self._checkpoint('entities',connection)
            linked=set()
            for entity in payload['entities']:
                eid=entities[refs[entity['ref']]]['id']
                if eid not in linked:
                    connection.execute('INSERT INTO memory_core.memory_entities(bot_id,memory_id,entity_id,mention_role) VALUES (%s,%s,%s,%s)',(self.space_id,card['id'],eid,entity['mention_role']))
                    linked.add(eid)
            self._checkpoint('bridges',connection)
            for relation in payload['relations']:
                src=entities[refs[relation['source_ref']]]['id']; dst=entities[refs[relation['target_ref']]]['id']
                if src==dst:
                    continue
                connection.execute('''INSERT INTO memory_core.relations
                    (bot_id,relation_key,source_entity_id,target_entity_id,relation_type,relation_type_raw,description,valid_from,source_memory_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (self.space_id,'review:'+uuid4().hex,src,dst,relation['relation_type'],relation['relation_type_raw'],relation['description'],relation['valid_from'],card['id']))
            self._checkpoint('relations',connection)
            for tid in payload['topic_ids']:
                connection.execute('INSERT INTO memory_core.memory_topics(bot_id,memory_id,topic_id) VALUES (%s,%s,%s)',(self.space_id,card['id'],tid))
            connection.execute('UPDATE memory_core.memory_items SET primary_topic_id=%s WHERE id=%s AND bot_id=%s',(payload['topic_ids'][0],card['id'],self.space_id))
            self._checkpoint('topics',connection)
            topics=[t for t in current['topics'] if t['id'] in payload['topic_ids']]
            rows=build_rows(card,list(entities.values()),topics,timestamp=card['created_at'],ignored_terms=current['ignored_terms'])
            connection.execute('SELECT memory_core.replace_memory_keyword_tokens(%s,%s,%s)',(card['id'],self.space_id,Jsonb(rows)))
            self._checkpoint('keywords',connection)
            connection.execute('''UPDATE memory_core.pending_memories SET status='approved',memory_item_id=%s,review_key=%s,
                reviewed_at=memory_core.format_timestamp(CURRENT_TIMESTAMP),version=version+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE id=%s AND bot_id=%s''',
                (card['id'],request_key,draft_id,self.space_id))
            self._checkpoint('review',connection)
            return self._result(self._draft(connection,draft_id))

    @staticmethod
    def _result(draft):
        return {k:draft[k] for k in ('id','status','version','memory_item_id','duplicate_memory_id','review_key','dedup_state')}

    def card_source(self, memory_id):
        identifier(memory_id,'memory_id')
        with connect(self.dsn) as connection:
            draft=connection.execute("SELECT id FROM memory_core.pending_memories WHERE bot_id=%s AND memory_item_id=%s AND status='approved'",(self.space_id,memory_id)).fetchone()
            if draft is None:
                raise ValueError('卡片没有本空间的审核来源')
        return self.source(draft['id'])
