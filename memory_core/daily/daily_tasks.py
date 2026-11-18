"""Durable per-space daily task keys; model calls never hold a transaction."""

from contextlib import contextmanager
from datetime import timedelta

from psycopg.types.json import Jsonb

from ..storage.clock import format_timestamp
from ..storage.db import background_lock, connect
from ..storage.postgres import PostgresStore
from ..storage.schema import DIARY_VERSION


class TaskLost(ValueError):
    code = "daily_task_lost"


def require_task(connection, task):
    row = connection.execute(
        """SELECT attempts,state,result FROM memory_core.daily_tasks WHERE bot_id=%s
        AND task_kind=%s AND business_date=%s FOR UPDATE""",
        (task["bot_id"], task["task_kind"], task["business_date"]),
    ).fetchone()
    if not row or row["state"] != "running" or row["attempts"] != task["attempts"]:
        raise TaskLost("每日任务的执行权已失效")
    return row


def finish_task(
    connection, task, timestamp, *, state="done", result=None, code=None, retry_at=None
):
    previous = require_task(connection, task)
    if result is not None:
        result = {**(previous["result"] or {}), **result}
    else:
        result = previous["result"]
    connection.execute(
        """UPDATE memory_core.daily_tasks SET state=%s,result=%s,last_error_code=%s,
        retry_at=%s,finished_at=%s,updated_at=%s WHERE bot_id=%s AND task_kind=%s AND business_date=%s""",
        (
            state,
            Jsonb(result) if result is not None else None,
            code,
            retry_at,
            timestamp,
            timestamp,
            task["bot_id"],
            task["task_kind"],
            task["business_date"],
        ),
    )


class DailyTasks(PostgresStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        with connect(dsn) as c:
            if (
                c.execute("SELECT version FROM memory_core.installation").fetchone()[
                    "version"
                ]
                < DIARY_VERSION
            ):
                raise ValueError("每日任务需要先升级记忆核心到版本 13")

    @contextmanager
    def run_lock(self):
        with background_lock(self.dsn, "conversation-daily:" + self.space_id) as guard:
            yield guard is not None

    def claim(self, kind, day, *, now):
        """Caller owns run_lock; attempt fencing rejects late, disconnected workers."""
        timestamp = format_timestamp(now, self.settings)
        with connect(self.dsn) as c:
            return c.execute(
                """INSERT INTO memory_core.daily_tasks
                (bot_id,task_kind,business_date,state,attempts,started_at,updated_at)
                VALUES (%s,%s,%s,'running',1,%s,%s)
                ON CONFLICT (bot_id,task_kind,business_date) DO UPDATE SET state='running',
                attempts=daily_tasks.attempts+1,started_at=EXCLUDED.started_at,updated_at=EXCLUDED.updated_at,
                finished_at=NULL,retry_at=NULL
                WHERE daily_tasks.state IN ('running','failed')
                AND (daily_tasks.retry_at IS NULL OR daily_tasks.retry_at::timestamptz<=%s)
                RETURNING *""",
                (self.space_id, kind, day, timestamp, timestamp, now),
            ).fetchone()

    def finish(self, task, *, now, state="done", result=None):
        if task["bot_id"] != self.space_id:
            raise ValueError("任务属于其他空间")
        with connect(self.dsn) as c:
            finish_task(
                c,
                task,
                format_timestamp(now, self.settings),
                state=state,
                result=result,
            )

    def fail(self, task, code, *, now):
        if task["bot_id"] != self.space_id:
            raise ValueError("任务属于其他空间")
        with connect(self.dsn) as c:
            finish_task(
                c,
                task,
                format_timestamp(now, self.settings),
                state="failed",
                code=code,
                retry_at=format_timestamp(now + timedelta(minutes=5), self.settings),
            )

    def record_usage(self, task, event, *, now, connection=None):
        if task['bot_id'] != self.space_id or event.get('stage') not in ('selection','structure','topic_summary','receipt_judge'):
            raise ValueError('用量记录与任务不符')
        if event['stage']=='topic_summary' and task['task_kind']!='topic_summaries':
            raise ValueError('主题总结用量不属于当前任务')
        if event['stage']=='receipt_judge' and task['task_kind']!='receipts':
            raise ValueError('回执补判用量不属于当前任务')
        event = dict(event, attempt=task['attempts'], occurred_at=format_timestamp(now,self.settings))
        def save(c):
            require_task(c,task)
            c.execute('''UPDATE memory_core.daily_tasks SET model_usage=model_usage || %s::jsonb
                WHERE bot_id=%s AND task_kind=%s AND business_date=%s''',
                (Jsonb([event]),self.space_id,task['task_kind'],task['business_date']))
            return event
        if connection is not None:
            return save(connection)
        with connect(self.dsn) as c:
            return save(c)

    def progress(self, task, values, *, connection=None):
        if task["bot_id"] != self.space_id:
            raise ValueError("任务属于其他空间")

        def save(c):
            require_task(c, task)
            c.execute(
                """UPDATE memory_core.daily_tasks SET result=coalesce(result,'{}'::jsonb) || %s::jsonb
                WHERE bot_id=%s AND task_kind=%s AND business_date=%s""",
                (
                    Jsonb(values),
                    self.space_id,
                    task["task_kind"],
                    task["business_date"],
                ),
            )

        if connection is not None:
            return save(connection)
        with connect(self.dsn) as c:
            save(c)
