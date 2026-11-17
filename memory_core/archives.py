"""Reversible archival; normal card contents and retrieval indexes stay intact."""

from .archive_policy import ArchiveSettings, receipt_health
from .clock import format_timestamp
from .daily_tasks import finish_task, require_task
from .db import connect
from .extraction_contract import identifier
from .postgres import PostgresStore
from .schema import ARCHIVE_VERSION
from .space import RevisionConflict


class ArchiveStore(PostgresStore):
    def __init__(self, dsn, space_id, policy=None):
        super().__init__(dsn, space_id)
        if self.schema_version < ARCHIVE_VERSION:
            raise ValueError('归档与恢复前请先升级 memory_core 到版本 17')
        self.policy = policy or ArchiveSettings.from_env()

    @staticmethod
    def _checkpoint(stage, connection):
        """Fault-injection seam for transaction and concurrency tests."""

    def archive_task(self, task, *, now):
        if task['bot_id'] != self.space_id or task['task_kind'] != 'archive':
            raise ValueError('归档任务不属于当前空间')
        timestamp = format_timestamp(now, self.settings)
        with connect(self.dsn) as connection:
            require_task(connection, task)
            health = receipt_health(connection, self.space_id, self.policy, now)
            archived = 0
            if not health['paused']:
                self._checkpoint('before_lock', connection)
                # Evaluate after acquiring each row lock, so a concurrent use,
                # restoration or property edit cannot be overwritten by an old plan.
                cards = connection.execute('''SELECT id,item_kind,archive_reason,parent_reflection_id,
                    gardener_result,decay_layer,is_milestone,created_at,last_used_at,restored_at,primary_topic_id
                    FROM memory_core.memory_items WHERE bot_id=%s AND archive_reason IS NULL
                    ORDER BY id FOR UPDATE''', (self.space_id,)).fetchall()
                topics = connection.execute(
                    'SELECT id,preset_key FROM memory_core.topics WHERE bot_id=%s', (self.space_id,)
                ).fetchall()
                topic_keys = {topic['id']: topic['preset_key'] for topic in topics}
                for card in cards:
                    card['primary_topic_key'] = topic_keys.get(card['primary_topic_id'])
                    if self.policy.due(card, now):
                        connection.execute('''UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=%s
                            WHERE bot_id=%s AND id=%s''', (timestamp, self.space_id, card['id']))
                        archived += 1
                self._checkpoint('archive', connection)
            result = dict(archived_count=archived, receipt_health=health)
            finish_task(connection, task, timestamp, state='skipped' if health['paused'] else 'done', result=result)
            return result

    def restore(self, card_id, *, expected_archived_at, now):
        identifier(card_id, 'card_id')
        timestamp = format_timestamp(now, self.settings)
        with connect(self.dsn) as connection:
            card = connection.execute('''SELECT id,archive_reason,archived_at,restored_at
                FROM memory_core.memory_items WHERE bot_id=%s AND id=%s FOR UPDATE''',
                (self.space_id, card_id)).fetchone()
            if card is None:
                raise ValueError('卡片已删除、不存在或属于其他空间')
            if card['archive_reason'] is None:
                return dict(id=card_id, restored=False, restored_at=card['restored_at'])
            if card['archived_at'] != expected_archived_at:
                raise RevisionConflict('归档状态已改变，请刷新后再恢复')
            connection.execute('''UPDATE memory_core.memory_items SET archive_reason=NULL,archived_at=NULL,restored_at=%s
                WHERE bot_id=%s AND id=%s''', (timestamp, self.space_id, card_id))
            self._checkpoint('restore', connection)
            return dict(id=card_id, restored=True, restored_at=timestamp)
