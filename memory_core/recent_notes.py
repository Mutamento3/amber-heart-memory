"""Versioned recent notes and latest secretary job, bound to one space."""

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb

from .clock import format_timestamp
from .daily_tasks import finish_task, require_task
from .db import connect
from .postgres import PostgresStore
from .recent_note_text import SECTIONS, char_count
from .recent_note_tidy import build_entries
from .schema import RECENT_NOTES_VERSION
from .space import RevisionConflict, read_policy


class RecentNotesStore(PostgresStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        if self.schema_version < RECENT_NOTES_VERSION:
            raise ValueError('使用记忆书签前请先升级记忆核心到版本 20')

    def timestamp(self, now=None):
        return format_timestamp(now or datetime.now(timezone.utc), self.settings)

    def policy(self):
        with connect(self.dsn) as c:
            return read_policy(c, self.space_id)

    def _empty(self):
        return dict(bot_id=self.space_id, version=0, tidy_job={}, **dict.fromkeys(SECTIONS, ''))

    def _lock(self, c):
        c.execute('INSERT INTO memory_core.recent_notes(bot_id) VALUES (%s) ON CONFLICT DO NOTHING', (self.space_id,))
        return c.execute('SELECT * FROM memory_core.recent_notes WHERE bot_id=%s FOR UPDATE', (self.space_id,)).fetchone()

    def read(self, policy):
        with connect(self.dsn) as c:
            row = c.execute('SELECT * FROM memory_core.recent_notes WHERE bot_id=%s', (self.space_id,)).fetchone() or self._empty()
        sections = {section: row[section] for section in SECTIONS}
        counts = {section: char_count(value) for section, value in sections.items()}
        total = sum(counts.values())
        return dict(sections=sections, version=row['version'], counts=counts, total_chars=total,
                    over_limit=total > policy.soft_limit, soft_limit=policy.soft_limit, target=policy.target,
                    tidy_job=row['tidy_job'] or dict(status='idle'))

    def save(self, sections, expected_version, *, tidy_job_id=None, now=None):
        if (not isinstance(sections, dict) or not sections or set(sections)-SECTIONS.keys()
                or any(not isinstance(content, str) for content in sections.values())):
            raise ValueError('记忆书签分区或内容无效')
        with connect(self.dsn) as c:
            row = self._lock(c)
            if row['version']!=expected_version:
                raise RevisionConflict('记忆书签已改变，请重新整理或核对最新内容后再保存，避免覆盖新内容')
            job = row['tidy_job']
            if tidy_job_id is not None:
                if (job.get('id')!=tidy_job_id or job.get('status')!='done'
                        or job.get('base_version')!=expected_version):
                    raise RevisionConflict('整理结果已改变，请重新整理')
                job = dict(job, status='applied', saved_at=self.timestamp(now))
            updated = {section: sections.get(section, row[section]) for section in SECTIONS}
            changed = any(updated[section]!=row[section] for section in SECTIONS)
            c.execute('''UPDATE memory_core.recent_notes SET period=%s,health=%s,longterm=%s,daily=%s,
                version=version+%s,updated_at=CASE WHEN %s THEN %s ELSE updated_at END,tidy_job=%s WHERE bot_id=%s''',
                (*updated.values(), int(changed), changed, self.timestamp(now), Jsonb(job), self.space_id))
            return dict(version=row['version']+changed)

    @staticmethod
    def _checkpoint(stage, connection):
        """Transaction fault-injection seam for isolated acceptance."""

    def absorb(self, task, *, now):
        if task['bot_id']!=self.space_id or task['task_kind']!='recent_notes':
            raise ValueError('书签整理任务不属于当前空间')
        stamp = self.timestamp(now)
        with connect(self.dsn) as c:
            require_task(c, task)
            row = self._lock(c)
            bookmarks = c.execute('''SELECT * FROM memory_core.moment_bookmarks
                WHERE bot_id=%s AND business_date<%s ORDER BY created_at::timestamptz DESC,id DESC FOR UPDATE''',
                (self.space_id, task['business_date'])).fetchall()
            if bookmarks:
                sections = {section: '\n'.join([*[b['content'] for b in bookmarks if b['section']==section],
                    *([row[section]] if row[section] else [])]) for section in SECTIONS}
                c.execute('''UPDATE memory_core.recent_notes SET period=%s,health=%s,longterm=%s,daily=%s,
                    version=version+1,updated_at=%s WHERE bot_id=%s''',
                    (*sections.values(), stamp, self.space_id))
                self._checkpoint('sections', c)
                changed = c.execute('DELETE FROM memory_core.moment_bookmarks WHERE bot_id=%s AND id=ANY(%s)',
                    (self.space_id, [b['id'] for b in bookmarks])).rowcount
                if changed!=len(bookmarks):
                    raise ValueError('待整理书签数量已改变，请重试')
                self._checkpoint('bookmarks', c)
            result = dict(bookmarks=len(bookmarks), version=row['version'] + bool(bookmarks))
            finish_task(c, task, stamp, result=result, state='done' if bookmarks else 'skipped')
        return result

    @contextmanager
    def tidy_lock(self):
        # A session lock spans the model wait, without a DB transaction or the
        # proxy round lock. Recovery can distinguish a live worker from a restart.
        with connect(self.dsn) as c:
            key = 'recent-note-tidy:' + self.space_id
            acquired = c.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked', (key,)).fetchone()['locked']
            c.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    c.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))
                    c.commit()

    def recover(self):
        with connect(self.dsn) as c:
            locked = c.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked',
                ('recent-note-tidy:' + self.space_id,)).fetchone()['locked']
            if not locked:
                return
            row = self._lock(c)
            job = row['tidy_job']
            if job.get('status')=='running':
                job.update(status='failed', error='服务重启，整理中断，请重新整理', finished_at=self.timestamp())
                self._save_job(c, job)

    def _save_job(self, c, job):
        c.execute('UPDATE memory_core.recent_notes SET tidy_job=%s WHERE bot_id=%s', (Jsonb(job), self.space_id))

    def begin_tidy(self, policy):
        with connect(self.dsn) as c:
            row = self._lock(c)
            if row['tidy_job'].get('status')=='running':
                return row['tidy_job'], False
            sections = {section: row[section] for section in SECTIONS}
            job = dict(id=str(uuid4()), status='running', base_version=row['version'], base_sections=sections,
                entries=build_entries(sections), suggestions=[], need_confirm=[], dropped=[], note='', error='',
                soft_limit=policy.soft_limit, target=policy.target, started_at=self.timestamp(), finished_at=None)
            self._save_job(c, job)
        return job, True

    def finish_tidy(self, identifier, values):
        with connect(self.dsn) as c:
            row = self._lock(c)
            job = row['tidy_job']
            if job.get('id')!=identifier or job.get('status')!='running':
                return False
            job.update(values, finished_at=self.timestamp())
            self._save_job(c, job)
        return True

    def cancel_tidy(self):
        with connect(self.dsn) as c:
            row = self._lock(c)
            job = row['tidy_job']
            if job.get('status')=='running':
                job.update(status='cancelled', error='已取消本次整理', finished_at=self.timestamp())
                self._save_job(c, job)
            return job or dict(status='idle')
