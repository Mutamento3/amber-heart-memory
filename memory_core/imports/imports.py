"""Durable preview, provenance and progress; extraction stays in DraftStore."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..storage.clock import format_timestamp
from ..storage.db import background_lock, connect
from ..cards.drafts import DraftStore, ExtractionConfigurationChanged, MaterialExpired, context_digest, context_for
from ..cards.extraction_contract import check_credentials, material_input
from .import_formats import ImportDay, ImportFormatError, SOURCES
from .import_preview import preview
from ..storage.schema import (DEEPSEEK_IMPORT_VERSION, IMPORT_APPROVAL_VERSION, IMPORT_LIFECYCLE_VERSION, IMPORT_PROGRESS_VERSION,
    IMPORT_VERSION, KELIVO_IMPORT_VERSION, STRUCTURE_CLAIM_VERSION)
from ..storage.space import RevisionConflict, read_policy

ACTIVE = ('queued', 'running')
PAUSE_ERRORS = ('import_configuration_changed', 'import_material_expired')
IMPORT_APPROVAL_FAILED = 'import_approval_failed'
IMPORT_ENDED = 'import_ended'
IMPORT_RUN_LOCK_KEY = 'memory-import-runner'


def direct_import_draft_filter(version, *, in_progress=False):
    """Exclude direct-import drafts, or only ones still owned by their worker."""
    if version < IMPORT_APPROVAL_VERSION:
        return ''
    active = ''' AND i.status IN ('pending','running') AND j.status IN ('queued','running') ''' if in_progress else ''
    return ''' AND NOT EXISTS (SELECT 1 FROM memory_core.import_items i
        JOIN memory_core.import_jobs j ON (j.id,j.bot_id)=(i.job_id,i.bot_id)
        WHERE i.bot_id=p.bot_id AND i.extraction_id=p.extraction_id AND j.auto_approve''' + active + ')'


class ImportStore(DraftStore):
    def __init__(self, dsn, space_id):
        super().__init__(dsn, space_id)
        if self.schema_version < IMPORT_VERSION:
            raise ValueError('导入需要先升级记忆核心到版本 22')

    def _job(self, c, identifier, *, lock=False):
        identifier=UUID(str(identifier))
        row=c.execute('SELECT * FROM memory_core.import_jobs WHERE id=%s AND bot_id=%s'
            + (' FOR UPDATE' if lock else ''),(identifier,self.space_id)).fetchone()
        if not row:
            raise ValueError('导入任务不存在或属于其他空间')
        return row

    def _items(self, c, job, *, include_memory_text=False):
        kelivo_columns = ',assistant_key,assistant_name,is_kelivo_memory' if job['source'] == 'kelivo' else ''
        if kelivo_columns and include_memory_text:
            kelivo_columns += ",CASE WHEN is_kelivo_memory THEN segments->0->>'text' END AS memory_text"
        return c.execute(f'''SELECT conversation_key,title,business_date,source_key,message_count{kelivo_columns}
            FROM memory_core.import_items WHERE job_id=%s AND bot_id=%s ORDER BY ordinal''',
            (job['id'],self.space_id)).fetchall()

    def _preview(self, c, rows, **selection):
        items=[ImportDay(segments=None,**row) for row in rows]
        imported=c.execute('SELECT source_key FROM memory_core.source_materials WHERE bot_id=%s AND source_key=ANY(%s)',
            (self.space_id,[item.source_key for item in items])).fetchall()
        policy=read_policy(c,self.space_id)
        used=self.daily_candidate_counts(c,list({item.business_date for item in items if item.business_date is not None}))
        return preview(items,daily_limit=policy['daily_limit'],used_per_day=used,
            imported_keys=[row['source_key'] for row in imported],**selection)

    def create(self, source, items, *, now):
        if source not in SOURCES:
            raise ImportFormatError('导入来源与材料不符')
        kelivo = source == 'kelivo'
        if kelivo and self.schema_version < KELIVO_IMPORT_VERSION:
            raise ImportFormatError(f'请先升级记忆核心到版本 {KELIVO_IMPORT_VERSION}，再导入 Kelivo 备份')
        if source == 'deepseek' and self.schema_version < DEEPSEEK_IMPORT_VERSION:
            raise ImportFormatError(f'请先升级记忆核心到版本 {DEEPSEEK_IMPORT_VERSION}，再导入 DeepSeek 聊天记录')
        timestamp=format_timestamp(now,self.settings)
        with connect(self.dsn) as c:
            context=context_for(c,self.space_id)
            job=c.execute('''INSERT INTO memory_core.import_jobs
                (id,bot_id,source,policy_revision,context_digest,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (uuid4(),self.space_id,source,context['policy_revision'],context_digest(context),timestamp,timestamp)).fetchone()
            with c.cursor() as cursor:
                kelivo_columns = ',assistant_key,assistant_name,is_kelivo_memory' if kelivo else ''
                kelivo_values = ',%s,%s,%s' if kelivo else ''
                statement=f'''INSERT INTO memory_core.import_items
                    (job_id,bot_id,ordinal,conversation_key,title,business_date,source_key,message_count,segments,created_at,updated_at{kelivo_columns})
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s{kelivo_values})'''
                batch, size, total = [], 0, 0
                for ordinal,item in enumerate(items):
                    if item.source!=source or (item.is_kelivo_memory and not kelivo):
                        raise ImportFormatError('导入来源与材料不符')
                    material_input(item.segments,self.settings,item.business_date,
                                   allow_undated=kelivo and item.is_kelivo_memory and item.business_date is None)
                    check_credentials(item.title)
                    row = (job['id'],self.space_id,ordinal,item.conversation_key,item.title,item.business_date,
                        item.source_key,item.message_count,Jsonb(item.segments),timestamp,timestamp)
                    if kelivo:
                        check_credentials(item.assistant_name)
                        row += (item.assistant_key,item.assistant_name,item.is_kelivo_memory)
                    batch.append(row)
                    size += sum(len(segment['text'].encode('utf-8')) for segment in item.segments)
                    total += 1
                    if len(batch)>=64 or size>=1024*1024:
                        cursor.executemany(statement,batch)
                        batch, size = [], 0
                if batch:
                    cursor.executemany(statement,batch)
                if not total:
                    raise ImportFormatError('没有找到可导入的文字消息或记忆正文')
        return self.get(job['id'])

    def preview(self, identifier, **selection):
        with connect(self.dsn) as c:
            job=self._job(c,identifier)
            if job['status']!='preview':
                raise RevisionConflict('任务已确认，不能更改导入范围')
            initial = all(value is None for value in selection.values())
            rows=[dict(row,source=job['source']) for row in self._items(c,job,include_memory_text=initial)]
            return self._preview(c,rows,**selection)

    def confirm(self, identifier, *, now, auto_approve=False, **selection):
        timestamp=format_timestamp(now,self.settings)
        with connect(self.dsn) as c:
            job=self._job(c,identifier,lock=True)
            if job['status']!='preview':
                return self._describe(c,job,now=now)
            if type(auto_approve) is not bool:
                raise ValueError('直接收录必须是布尔值')
            if auto_approve and self.schema_version < IMPORT_APPROVAL_VERSION:
                raise ValueError('请先升级记忆核心到版本 23，再使用直接收录')
            current=context_for(c,self.space_id)
            if context_digest(current)!=job['context_digest']:
                raise RevisionConflict('导入设置已改变，请重新上传并预览')
            rows=[dict(row,source=job['source']) for row in self._items(c,job)]
            plan=self._preview(c,rows,**selection)
            chosen=plan['source_keys']
            # Persist the user's exact selection before making work runnable.
            c.execute('''UPDATE memory_core.import_items SET selected=source_key=ANY(%s),
                status=CASE WHEN source_key=ANY(%s) THEN 'pending' ELSE 'skipped' END,
                segments=CASE WHEN source_key=ANY(%s) THEN segments ELSE NULL END,updated_at=%s
                WHERE job_id=%s AND bot_id=%s''',(chosen,chosen,chosen,timestamp,job['id'],self.space_id))
            approval = ',auto_approve=%s' if self.schema_version >= IMPORT_APPROVAL_VERSION else ''
            params = ('queued' if chosen else 'completed',current['policy_revision'],timestamp)
            if approval:
                params += (auto_approve,)
            timing = ',run_started_at=%s' if chosen and self.schema_version >= IMPORT_PROGRESS_VERSION else ''
            if timing:
                params += (timestamp,)
            job=c.execute('UPDATE memory_core.import_jobs SET status=%s,policy_revision=%s,updated_at=%s' + approval + timing +
                ' WHERE id=%s AND bot_id=%s RETURNING *', (*params,job['id'],self.space_id)).fetchone()
            return self._describe(c,job)

    def _describe(self, c, job, *, now=None):
        counts=c.execute('''SELECT count(*) FILTER (WHERE selected) AS total,
            count(*) FILTER (WHERE selected AND status IN ('completed','skipped') AND error_code IS DISTINCT FROM %s) AS processed,
            count(*) FILTER (WHERE status='skipped') AS skipped,
            count(*) FILTER (WHERE selected AND status='failed') AS failed,
            count(*) FILTER (WHERE selected AND status='running') AS running,
            coalesce(sum(message_count) FILTER (WHERE selected),0) AS message_count,
            count(DISTINCT business_date) FILTER (WHERE selected) AS day_count,
            (SELECT count(*) FROM memory_core.import_items i JOIN memory_core.pending_memories p
                ON (p.extraction_id,p.bot_id)=(i.extraction_id,i.bot_id)
                WHERE i.job_id=%s AND i.bot_id=%s AND i.selected
                  AND p.review_ready_at IS NOT NULL) AS structured_cards
            FROM memory_core.import_items WHERE job_id=%s AND bot_id=%s''',
            (IMPORT_ENDED,job['id'],self.space_id,job['id'],self.space_id)).fetchone()
        handled = counts['processed'] + counts['failed']
        remaining = counts['total'] - handled
        if job['status'] == 'running' and job.get('run_started_at') and remaining > 0:
            completed_this_run = handled - job['run_processed_before']
            counts['estimated_remaining_seconds'] = None
            if completed_this_run > 0:
                now = now if now is not None else datetime.now(timezone.utc)
                elapsed = max(0, (now - datetime.fromisoformat(job['run_started_at'])).total_seconds())
                counts['estimated_remaining_seconds'] = elapsed / completed_this_run * remaining
        active=c.execute('''SELECT title,business_date,status,error_code FROM memory_core.import_items
            WHERE job_id=%s AND bot_id=%s AND selected AND status IN ('running','failed')
            ORDER BY (status='running') DESC,ordinal LIMIT 1''',
            (job['id'],self.space_id)).fetchone()
        # Progress polling needs the aggregate call count, not an ever-growing
        # copy of every billing event in a large historical import.
        visible={key:value for key,value in job.items() if key not in ('usage','run_started_at','run_processed_before')}
        return dict(visible,progress=dict(counts),current=active)

    def get(self, identifier, *, now=None):
        with connect(self.dsn) as c:
            return self._describe(c,self._job(c,identifier),now=now)

    def list_jobs(self, *, now=None):
        with connect(self.dsn) as c:
            rows=c.execute('SELECT * FROM memory_core.import_jobs WHERE bot_id=%s ORDER BY created_at::timestamptz DESC,id DESC LIMIT 20',
                (self.space_id,)).fetchall()
            return [self._describe(c,row,now=now) for row in rows]

    def retry(self, identifier, *, now):
        with connect(self.dsn) as c:
            job=self._job(c,identifier,lock=True)
            if job['status'] not in ('failed','completed'):
                return self._describe(c,job,now=now)
            if job['status']=='completed':
                result=self._describe(c,job)
                if not result['progress']['failed']:
                    return result
            current=context_for(c,self.space_id)
            if context_digest(current)!=job['context_digest']:
                raise RevisionConflict('导入期间设置已改变，已有候选需要人工复核；恢复原设置后可继续')
            missing=c.execute('''SELECT 1 FROM memory_core.import_items i
                LEFT JOIN memory_core.material_extractions e ON (e.id,e.bot_id)=(i.extraction_id,i.bot_id)
                LEFT JOIN memory_core.source_materials s ON (s.id,s.bot_id)=(e.material_id,e.bot_id)
                WHERE i.job_id=%s AND i.bot_id=%s AND i.selected AND i.status IN ('pending','running','failed')
                AND ((i.segments IS NULL AND i.extraction_id IS NULL) OR s.deleted_at IS NOT NULL) LIMIT 1''',
                (job['id'],self.space_id)).fetchone()
            if missing:
                raise ValueError('导入原材料已到保留期限，请重新上传')
            timestamp=format_timestamp(now,self.settings)
            c.execute("UPDATE memory_core.import_items SET status='pending',error_code=NULL,updated_at=%s WHERE job_id=%s AND bot_id=%s AND selected AND status='failed'",(timestamp,job['id'],self.space_id))
            timing, params = '', (timestamp,)
            if self.schema_version >= IMPORT_PROGRESS_VERSION:
                # The job lock also serializes item completion. Capture the exact starting
                # count even when a retry and a completion share the same timestamp.
                timing = ''',run_started_at=%s,run_processed_before=(
                    SELECT count(*) FROM memory_core.import_items i
                    WHERE i.job_id=j.id AND i.bot_id=j.bot_id AND i.selected
                      AND i.status IN ('completed','skipped'))'''
                params += (timestamp,)
            job=c.execute("UPDATE memory_core.import_jobs j SET status='queued',error_code=NULL,updated_at=%s" + timing +
                " WHERE j.id=%s AND j.bot_id=%s RETURNING j.*",(*params,job['id'],self.space_id)).fetchone()
            return self._describe(c,job)

    def stop(self, identifier, *, now):
        with connect(self.dsn) as c:
            job=self._job(c,identifier,lock=True)
            if job['status'] in ACTIVE:
                job=self._pause(c,job,'import_stopped',format_timestamp(now,self.settings))
            return self._describe(c,job)

    def _require_lifecycle(self):
        if self.schema_version < IMPORT_LIFECYCLE_VERSION:
            raise ValueError(f'放弃或结束导入需要先升级记忆核心到版本 {IMPORT_LIFECYCLE_VERSION}')

    def discard(self, identifier):
        self._require_lifecycle()
        with connect(self.dsn) as c:
            job = self._job(c, identifier, lock=True)
            discarded = c.execute('SELECT memory_core.discard_import_preview(%s,%s) AS value',
                (self.space_id, job['id'])).fetchone()['value']
            if not discarded:
                raise RevisionConflict('只有待确认的导入可以放弃')

    def end(self, identifier, *, now):
        self._require_lifecycle()
        timestamp = format_timestamp(now, self.settings)
        with connect(self.dsn) as c:
            job = self._job(c, identifier, lock=True)
            scope = (job['id'], self.space_id)
            if job['status'] == 'completed' and job['error_code'] == IMPORT_ENDED:
                return self._describe(c, job)
            if job['status'] != 'failed':
                raise RevisionConflict('只有已暂停的导入可以结束')
            if c.execute("""SELECT 1 FROM memory_core.import_items
                WHERE job_id=%s AND bot_id=%s AND status='running' LIMIT 1""", scope).fetchone():
                # Stop lets in-flight work finish. A free runner lock means
                # these rows survived an interrupted coordinator instead.
                idle = c.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS value',
                    (IMPORT_RUN_LOCK_KEY,)).fetchone()['value']
                if not idle:
                    raise RevisionConflict('还有正在处理的条目，请等它们收尾后再结束导入')
                c.execute("""UPDATE memory_core.import_items SET status='pending'
                    WHERE job_id=%s AND bot_id=%s AND status='running'""", scope)
            released = c.execute('SELECT memory_core.release_unfinished_import_materials(%s,%s) AS value',
                (self.space_id, job['id'])).fetchone()['value']
            if not released:
                raise RevisionConflict('还有草稿正在整理，请等它们收尾后再结束导入')
            c.execute("""UPDATE memory_core.import_items SET status='skipped',error_code=%s,segments=NULL,updated_at=%s
                WHERE job_id=%s AND bot_id=%s AND status IN ('pending','failed')""", (IMPORT_ENDED, timestamp, *scope))
            job = c.execute("""UPDATE memory_core.import_jobs SET status='completed',error_code=%s,updated_at=%s
                WHERE id=%s AND bot_id=%s RETURNING *""", (IMPORT_ENDED, timestamp, *scope)).fetchone()
            return self._describe(c, job)

    def recover_running(self, identifier, *, now):
        """Called by the new runner before starting any workers for this job."""
        with connect(self.dsn) as c:
            job=self._job(c,identifier,lock=True)
            if job['status'] in ACTIVE:
                c.execute('''UPDATE memory_core.import_items SET status='pending',updated_at=%s
                    WHERE job_id=%s AND bot_id=%s AND status='running' ''',
                    (format_timestamp(now,self.settings),job['id'],self.space_id))

    def _settle(self, c, job, timestamp):
        if job['status'] in ACTIVE:
            c.execute('''UPDATE memory_core.import_jobs j SET status='completed',error_code=NULL,updated_at=%s
                WHERE j.id=%s AND j.bot_id=%s AND NOT EXISTS (SELECT 1 FROM memory_core.import_items i
                    WHERE (i.job_id,i.bot_id)=(j.id,j.bot_id) AND i.selected AND i.status IN ('pending','running'))''',
                (timestamp,job['id'],self.space_id))

    def prepare_next(self, identifier, *, now):
        timestamp=format_timestamp(now,self.settings)
        with connect(self.dsn) as c:
            job=self._job(c,identifier,lock=True)
            if job['status'] not in ACTIVE:
                return None
            # The job row serializes claims, stop/retry and completion. Only this
            # short transaction holds it, while extraction runs independently.
            available = '''
                AND NOT EXISTS (SELECT 1 FROM memory_core.pending_memories p
                    WHERE (p.extraction_id,p.bot_id)=(i.extraction_id,i.bot_id)
                      AND p.status IN ('structure_pending','structure_failed')
                      AND p.structure_claim_until::timestamptz > clock_timestamp())''' if self.schema_version >= STRUCTURE_CLAIM_VERSION else ''
            item=c.execute('''SELECT i.* FROM memory_core.import_items i WHERE job_id=%s AND bot_id=%s
                AND selected AND status='pending' ''' + available + '''
                ORDER BY business_date,conversation_key,ordinal LIMIT 1 FOR UPDATE''',
                (job['id'],self.space_id)).fetchone()
            if not item:
                self._settle(c,job,timestamp)
                return None
            if context_digest(context_for(c,self.space_id))!=job['context_digest']:
                raise ExtractionConfigurationChanged('导入期间设置已改变')
            try:
                with c.transaction():
                    extraction_id=self._prepare_material(c,job,item)
            except MaterialExpired:
                raise
            except (ValueError, TypeError):
                self._set_item_result(c,item,'failed','import_prepare_failed',timestamp)
                self._settle(c,job,timestamp)
                return dict(item,skip=True,error_code='import_prepare_failed')
            if extraction_id is None:
                c.execute("UPDATE memory_core.import_items SET status='skipped',segments=NULL,updated_at=%s WHERE id=%s AND bot_id=%s",(timestamp,item['id'],self.space_id))
                self._settle(c,job,timestamp)
                return dict(item,skip=True)
            c.execute("UPDATE memory_core.import_jobs SET status='running',updated_at=%s WHERE id=%s AND bot_id=%s",(timestamp,job['id'],self.space_id))
            c.execute('''UPDATE memory_core.import_items SET extraction_id=%s,segments=NULL,status='running',updated_at=%s
                WHERE id=%s AND bot_id=%s''',(extraction_id,timestamp,item['id'],self.space_id))
            return dict(item,extraction_id=extraction_id,context_digest=job['context_digest'],auto_approve=job.get('auto_approve',False))

    def approval_failed(self, draft):
        with connect(self.dsn) as c:
            c.execute('''UPDATE memory_core.pending_memories SET error_code=%s
                WHERE bot_id=%s AND id=%s AND status='pending' AND version=%s''',
                (IMPORT_APPROVAL_FAILED,self.space_id,draft['id'],draft['version']))

    def _prepare_material(self, c, job, item):
        if item['extraction_id'] is not None:
            return item['extraction_id']
        # Coordinate with DraftStore.submit through the atomic handoff.
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
            ('source:'+self.space_id+':'+item['source_key'],))
        if c.execute('SELECT 1 FROM memory_core.source_materials WHERE bot_id=%s AND source_key=%s',
            (self.space_id,item['source_key'])).fetchone():
            return None
        if item['segments'] is None:
            raise MaterialExpired('导入原材料已过期')
        request_key='request:'+item['source_key']
        if job['source']=='memory' or item.get('is_kelivo_memory'):
            return self.submit_memory(item['source_key'],request_key,
                item['segments'][0]['text'],item['business_date'],connection=c)
        return self.submit(item['source_key'],request_key,item['segments'],
            business_date=item['business_date'],connection=c)

    def resume(self, item):
        extraction=self.extraction(item['extraction_id'])
        with connect(self.dsn) as c:
            current=context_for(c,self.space_id)
        if context_digest(current)!=item['context_digest']:
            raise ExtractionConfigurationChanged('导入期间设置已改变')
        if current['policy_revision']!=extraction['policy_revision']:
            self.reconfirm_extraction(extraction['id'],expected_version=extraction['version'],
                expected_policy_revision=current['policy_revision'],unchanged_context_digest=item['context_digest'])
        return extraction

    def finish_item(self, item, *, now, error=None):
        timestamp=format_timestamp(now,self.settings)
        with connect(self.dsn) as c:
            job=self._job(c,item['job_id'],lock=True)
            self._set_item_result(c,item,'failed' if error else 'completed',error,timestamp,previous='running')
            if error in PAUSE_ERRORS and job['status'] in ACTIVE:
                self._pause(c,job,error,timestamp)
            else:
                self._settle(c,job,timestamp)

    def defer_item(self, item, *, now):
        with connect(self.dsn) as c:
            self._job(c,item['job_id'],lock=True)
            self._set_item_result(c,item,'pending',None,format_timestamp(now,self.settings),previous='running')

    def _set_item_result(self, c, item, status, error, timestamp, *, previous='pending'):
        c.execute('''UPDATE memory_core.import_items SET status=%s,error_code=%s,updated_at=%s
            WHERE id=%s AND bot_id=%s AND job_id=%s AND status=%s''',
            (status,error,timestamp,item['id'],self.space_id,item['job_id'],previous))

    def record_usage(self, identifier, event, *, now):
        if event.get('stage') not in ('selection','structure'):
            raise ValueError('导入模型岗位无效')
        with connect(self.dsn) as c:
            self._job(c,identifier,lock=True)
            c.execute('''UPDATE memory_core.import_jobs SET model_calls=model_calls+1,usage=usage || %s::jsonb,
                updated_at=%s WHERE id=%s AND bot_id=%s''',
                (Jsonb([dict(event,occurred_at=format_timestamp(now,self.settings))]),format_timestamp(now,self.settings),identifier,self.space_id))

    def fail_job(self, identifier, code, *, now):
        if code not in PAUSE_ERRORS:
            raise ValueError('无效的导入暂停原因')
        with connect(self.dsn) as c:
            job=self._job(c,identifier,lock=True)
            if job['status'] in ACTIVE:
                self._pause(c,job,code,format_timestamp(now,self.settings))

    def _pause(self, c, job, code, timestamp):
        return c.execute('''UPDATE memory_core.import_jobs SET status='failed',error_code=%s,updated_at=%s
            WHERE id=%s AND bot_id=%s RETURNING *''',(code,timestamp,job['id'],self.space_id)).fetchone()

    def expire(self, *, now):
        """Import retention starts on upload; no live-chat history is involved."""
        timestamp=format_timestamp(now,self.settings)
        with connect(self.dsn) as c:
            days=read_policy(c,self.space_id)['source_retention_days']
            if days is None:
                return 0
            # Take the same job -> item lock order as prepare, stop and retry.
            jobs=c.execute('''SELECT j.id FROM memory_core.import_jobs j WHERE j.bot_id=%s AND EXISTS (
                SELECT 1 FROM memory_core.import_items i WHERE (i.job_id,i.bot_id)=(j.id,j.bot_id)
                AND i.segments IS NOT NULL AND i.created_at::timestamptz<=%s::timestamptz-make_interval(days=>%s))
                ORDER BY j.id FOR UPDATE''',(self.space_id,now,days)).fetchall()
            expired=[]
            if jobs:
                ids=[row['id'] for row in jobs]
                expired=c.execute('''UPDATE memory_core.import_items SET segments=NULL,
                    status=CASE WHEN selected AND status IN ('pending','running') THEN 'failed' ELSE status END,
                    error_code='import_material_expired',updated_at=%s WHERE bot_id=%s AND job_id=ANY(%s)
                    AND segments IS NOT NULL AND created_at::timestamptz<=%s::timestamptz-make_interval(days=>%s)
                    RETURNING job_id''',(timestamp,self.space_id,ids,now,days)).fetchall()
            if expired:
                c.execute('''UPDATE memory_core.import_jobs SET status='failed',error_code='import_material_expired',updated_at=%s
                    WHERE bot_id=%s AND id=ANY(%s) AND status IN ('preview','queued','running')''',
                    (timestamp,self.space_id,list({row['job_id'] for row in expired})))
            count=c.execute('''UPDATE memory_core.source_materials s SET segments=NULL,deleted_at=%s
                WHERE s.bot_id=%s AND s.deleted_at IS NULL
                AND s.created_at::timestamptz<=%s::timestamptz-make_interval(days=>%s)
                AND EXISTS (SELECT 1 FROM memory_core.material_extractions e JOIN memory_core.import_items i
                    ON (i.extraction_id,i.bot_id)=(e.id,e.bot_id) WHERE (e.material_id,e.bot_id)=(s.id,s.bot_id))''',
                (timestamp,self.space_id,now,days)).rowcount
            return len(expired)+count


def import_run_lock(dsn):
    """One coordinator across processes; crash releases this session lock."""
    return background_lock(dsn, IMPORT_RUN_LOCK_KEY)
