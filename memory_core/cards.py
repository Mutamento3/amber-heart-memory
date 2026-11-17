"""Model-free card metadata edits and permanent, transactional deletion."""

from psycopg.types.json import Jsonb

from .db import connect
from .drafts import context_for
from .extraction_contract import check_credentials, identifier, keys, text, validate_topics
from .graph_store import GraphStore
from .keyword_index import build_rows
from .schema import CARD_MUTATION_VERSION, TOPIC_SUMMARY_VERSION


class CardStore(GraphStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        with connect(dsn) as connection:
            if connection.execute('SELECT version FROM memory_core.installation').fetchone()['version']<CARD_MUTATION_VERSION:
                raise ValueError('修改或删除卡片前请先升级 memory_core 到版本 15')

    @staticmethod
    def _checkpoint(stage, connection):
        """Fault-injection seam for transaction rollback tests."""

    def _card(self, connection, card_id):
        identifier(card_id, 'card_id')
        row=connection.execute('SELECT * FROM memory_core.memory_items WHERE bot_id=%s AND id=%s FOR UPDATE',
            (self.space_id, card_id)).fetchone()
        if row is None:
            raise ValueError('卡片已删除、不存在或属于其他空间')
        if row['item_kind']!='atomic':
            raise ValueError('主题总结首发只读，不能直接修改或删除')
        return row

    def edit(self, card_id, payload):
        keys(payload, ('title','topic_ids','is_milestone'), 'card_edit')
        title=text(payload['title'], 'title')
        if len(title)>300:
            raise ValueError('标题最多 300 字')
        if type(payload['is_milestone']) is not bool:
            raise ValueError('里程碑必须为布尔值')
        check_credentials(payload)
        with connect(self.dsn) as connection:
            context=context_for(connection, self.space_id)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('review:'+self.space_id,))
            self._card(connection, card_id)
            # Reuse the exact extraction/review topic rules, including fallback.
            ids=validate_topics(payload['topic_ids'], context)
            card=connection.execute('''UPDATE memory_core.memory_items SET title=%s,is_milestone=%s,primary_topic_id=%s
                WHERE bot_id=%s AND id=%s RETURNING *''',
                (title, payload['is_milestone'], ids[0], self.space_id, card_id)).fetchone()
            connection.execute('DELETE FROM memory_core.memory_topics WHERE bot_id=%s AND memory_id=%s', (self.space_id, card_id))
            for tid in ids:
                connection.execute('INSERT INTO memory_core.memory_topics(bot_id,memory_id,topic_id) VALUES (%s,%s,%s)',
                    (self.space_id, card_id, tid))
            self._checkpoint('topics', connection)
            entities=connection.execute('''SELECT e.* FROM memory_core.entities e JOIN memory_core.memory_entities me
                ON (me.entity_id,me.bot_id)=(e.id,e.bot_id) WHERE me.bot_id=%s AND me.memory_id=%s''',
                (self.space_id, card_id)).fetchall()
            topics=[t for t in context['topics'] if t['id'] in ids]
            rows=build_rows(card, entities, topics, timestamp=card['updated_at'], ignored_terms=context['ignored_terms'])
            connection.execute('SELECT memory_core.replace_memory_keyword_tokens(%s,%s,%s)', (card_id,self.space_id,Jsonb(rows)))
            self._checkpoint('keywords', connection)
            return dict(id=card_id)

    def delete(self, card_id):
        with connect(self.dsn) as connection:
            context=context_for(connection, self.space_id)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('review:'+self.space_id,))
            self._card(connection, card_id)
            candidates=[r['id'] for r in connection.execute('''
                SELECT entity_id AS id FROM memory_core.memory_entities WHERE bot_id=%s AND memory_id=%s
                UNION SELECT source_entity_id FROM memory_core.relations WHERE bot_id=%s AND source_memory_id=%s
                UNION SELECT target_entity_id FROM memory_core.relations WHERE bot_id=%s AND source_memory_id=%s''',
                (self.space_id,card_id,self.space_id,card_id,self.space_id,card_id))]
            connection.execute('''UPDATE memory_core.pending_memories SET status='deleted',narrative='{}',payload=NULL,
                memory_item_id=NULL,duplicate_memory_id=NULL,error_code='card_deleted',dedup_state=NULL,
                policy_snapshot=NULL,version=version+1,updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP)
                WHERE bot_id=%s AND (memory_item_id=%s OR duplicate_memory_id=%s)''',
                (self.space_id,card_id,card_id))
            self._checkpoint('drafts', connection)
            if self.schema_version >= TOPIC_SUMMARY_VERSION:
                connection.execute('''UPDATE memory_core.topic_summary_sources SET memory_id=NULL,source_hash=NULL
                    WHERE bot_id=%s AND memory_id=%s''',(self.space_id,card_id))
            connection.execute('DELETE FROM memory_core.relations WHERE bot_id=%s AND source_memory_id=%s', (self.space_id,card_id))
            # Embedding lives on the card; keyword/entity/topic bridges cascade.
            connection.execute('DELETE FROM memory_core.memory_items WHERE bot_id=%s AND id=%s', (self.space_id,card_id))
            self._checkpoint('card', connection)
            cleanup_orphans(connection,self.space_id,candidates,[p['id'] for p in context['participants'].values()])
            self._checkpoint('entities', connection)
            # Parent summaries and original conversations are historical records.
            # No fingerprint is retained: a later re-extraction/import may re-add it.
            return dict(id=card_id,deleted=True)


def cleanup_orphans(connection, space_id, candidates, protected):
    connection.execute('''DELETE FROM memory_core.entities e WHERE e.bot_id=%s AND e.id=ANY(%s) AND NOT(e.id=ANY(%s))
        AND NOT EXISTS(SELECT 1 FROM memory_core.memory_entities me WHERE (me.entity_id,me.bot_id)=(e.id,e.bot_id))
        AND NOT EXISTS(SELECT 1 FROM memory_core.relations r WHERE r.bot_id=e.bot_id AND e.id IN (r.source_entity_id,r.target_entity_id))
        AND NOT EXISTS(SELECT 1 FROM memory_core.pending_memories p,
            LATERAL jsonb_array_elements(p.payload->'entities') ref
            WHERE p.bot_id=e.bot_id AND p.status IN ('pending','structure_pending','structure_failed')
            AND ref->>'known_entity_id'=e.id::text)''',
        (space_id,candidates,protected))
