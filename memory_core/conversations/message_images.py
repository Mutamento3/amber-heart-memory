"""One final caption write per message; model calls happen outside transactions."""

from datetime import datetime,timedelta,timezone
from psycopg.types.json import Jsonb
from .conversations import ConversationStore
from ..storage.db import connect
from ..cards.extraction_contract import identifier
from .image_descriptions import validate_results
from ..storage.schema import IMAGE_DESCRIPTION_VERSION
from ..storage.space import read_policy


class MessageImages(ConversationStore):
    def __init__(self,dsn,space):
        super().__init__(dsn,space)
        if self.schema_version<IMAGE_DESCRIPTION_VERSION:
            raise ValueError('图片描述需要先升级记忆核心到版本 19')

    def source(self,record,*,now=None):
        if record['step_index']!=0:
            return None
        with connect(self.dsn) as c:
            policy=read_policy(c,self.space_id)
            row=c.execute('''SELECT m.*,t.created_at AS turn_created_at FROM memory_core.conversation_messages m
                JOIN memory_core.conversation_turns t ON (t.id,t.bot_id)=(m.turn_id,m.bot_id)
                WHERE m.bot_id=%s AND m.turn_id=%s AND m.request_id=%s AND m.role='user' AND m.event_index=0''',
                (self.space_id,record['turn_id'],record['request_id'])).fetchone()
            if not self.available(row,policy,now):
                return None
            return dict(message_id=row['id'],image_count=sum(p['type']=='image_placeholder' for p in row['content_parts']),
                participants=policy['participants'])

    @staticmethod
    def available(row,policy,now):
        if not row or row['content_deleted_at'] is not None or row['image_descriptions'] is not None:
            return False
        retention=policy['conversation_retention_days']
        now=now or datetime.now(timezone.utc)
        return (retention is None or datetime.fromisoformat(row['turn_created_at'])>now-timedelta(days=retention))

    def publish(self,message_id,results,*,now=None):
        identifier(message_id,'message_id')
        with connect(self.dsn) as c:
            policy=read_policy(c,self.space_id)
            self._lock(c)
            row=c.execute('''SELECT m.*,t.created_at AS turn_created_at,t.position_id,
                t.context_manifest->>'mode' AS context_mode FROM memory_core.conversation_messages m
                JOIN memory_core.conversation_turns t ON (t.id,t.bot_id)=(m.turn_id,m.bot_id)
                WHERE m.bot_id=%s AND m.id=%s AND m.role='user' FOR UPDATE OF m''',(self.space_id,message_id)).fetchone()
            if not self.available(row,policy,now):
                return False
            validate_results(results,sum(p['type']=='image_placeholder' for p in row['content_parts']))
            c.execute('UPDATE memory_core.conversation_messages SET image_descriptions=%s WHERE bot_id=%s AND id=%s',
                (Jsonb(results),self.space_id,message_id))
            if row['context_mode'] == 'hook':
                # Regeneration keeps the original user's images, even if the
                # description job finishes after the replacement has committed.
                c.execute('''UPDATE memory_core.conversation_messages m SET image_descriptions=%s
                    FROM memory_core.conversation_turns t
                    WHERE (t.id,t.bot_id)=(m.turn_id,m.bot_id) AND t.bot_id=%s AND t.position_id=%s
                    AND t.context_manifest->>'mode'='hook' AND m.role='user'
                    AND m.content_deleted_at IS NULL AND m.image_descriptions IS NULL''',
                    (Jsonb(results),self.space_id,row['position_id']))
            if any(r['description'] for r in results):
                c.execute('UPDATE memory_core.conversation_space_state SET history_revision=history_revision+1 WHERE bot_id=%s',(self.space_id,))
            return True
