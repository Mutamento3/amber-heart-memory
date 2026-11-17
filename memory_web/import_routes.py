"""Administrator import uploads share the existing Origin, session and CSRF guard."""

from contextlib import asynccontextmanager
import re
from tempfile import TemporaryFile, TemporaryDirectory
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr
from starlette.concurrency import run_in_threadpool

from memory_core.import_formats import MAX_UPLOAD_BYTES, MAX_JSON_BYTES
from memory_core.import_stream import iter_upload
from memory_core.imports import ImportStore
from memory_core.schema import IMPORT_APPROVAL_VERSION
from .import_jobs import ImportJobs


def is_import_upload(request):
    return request.method=='POST' and re.fullmatch(r'/api/spaces/[^/]+/imports/upload',request.url.path) is not None


class ImportSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    selected_conversations: list[StrictStr] | None = None
    date_start: StrictStr | None = None
    date_end: StrictStr | None = None


class ImportStop(BaseModel):
    model_config=ConfigDict(extra='forbid')


class ImportConfirm(ImportSelection):
    auto_approve: StrictBool = False


def attach_imports(app, settings, require_space, *, enabled, model=None, clock=None, embedder=None, embedding_model='unconfigured'):
    jobs=ImportJobs(settings,model=model,clock=clock,embedder=embedder,embedding_model=embedding_model)
    app.state.imports=jobs

    def store(space):
        require_space(space)
        if not enabled:
            raise HTTPException(503,'请先升级记忆核心到版本 22，再使用导入')
        return ImportStore(settings.dsn,space)

    @app.get('/api/spaces/{space}/imports')
    def listing(space: str):
        source=store(space)
        return dict(configured=jobs.model.configured,max_upload_bytes=MAX_UPLOAD_BYTES,
            max_json_bytes=MAX_JSON_BYTES,auto_approve_supported=source.schema_version>=IMPORT_APPROVAL_VERSION,
            jobs=source.list_jobs(now=jobs.clock()))

    @app.post('/api/spaces/{space}/imports/upload')
    async def upload(space: str, request: Request,
                     source: Literal['chatgpt','claude','gemini','memory'],
                     filename: Annotated[str,Query(min_length=1,max_length=512)]):
        target=await run_in_threadpool(store,space)
        size=0
        # The temporary file has no persistent filename and is closed on every
        # exit, including malformed uploads and a disconnected browser.
        # The normal state directory is a private disk volume. /tmp is tmpfs
        # in the default container: staging a large import there uses its RAM.
        with TemporaryDirectory(prefix='.import-',dir=settings.password_file.parent) as scratch, TemporaryFile(dir=scratch) as temporary:
            async for chunk in request.stream():
                size+=len(chunk)
                if size>MAX_UPLOAD_BYTES:
                    raise HTTPException(413,'上传文件超过 90 MiB 上限，请将 JSON 单独压成 ZIP 后上传')
                await run_in_threadpool(temporary.write,chunk)

            def prepare():
                temporary.seek(0)
                items=iter_upload(source,[(filename,temporary)],target.settings,temp_dir=scratch)
                return target.create(source,items,now=jobs.clock())

            return await run_in_threadpool(prepare)

    @app.get('/api/spaces/{space}/imports/{identifier}')
    def read(space: str, identifier: UUID):
        return store(space).get(identifier,now=jobs.clock())

    @app.post('/api/spaces/{space}/imports/{identifier}/preview')
    def selection(space: str, identifier: UUID, body: ImportSelection):
        return store(space).preview(identifier,**body.model_dump())

    @app.post('/api/spaces/{space}/imports/{identifier}/confirm')
    async def confirm(space: str, identifier: UUID, body: ImportConfirm):
        if not jobs.model.configured:
            raise HTTPException(503,'请先配置抽卡模型，再确认导入')
        target=await run_in_threadpool(store,space)
        result=await run_in_threadpool(target.confirm,identifier,now=jobs.clock(),**body.model_dump())
        jobs.wakeup.set()
        return result

    @app.post('/api/spaces/{space}/imports/{identifier}/retry')
    async def retry(space: str, identifier: UUID, body: ImportSelection):
        if body.model_dump(exclude_none=True):
            raise HTTPException(400,'重试不能更改原导入范围')
        if not jobs.model.configured:
            raise HTTPException(503,'请先配置抽卡模型，再重试导入')
        target=await run_in_threadpool(store,space)
        result=await run_in_threadpool(target.retry,identifier,now=jobs.clock())
        jobs.wakeup.set()
        return result

    @app.post('/api/spaces/{space}/imports/{identifier}/stop')
    def stop(space: str, identifier: UUID, body: ImportStop):
        return store(space).stop(identifier,now=jobs.clock())

    original=app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            if enabled:
                await jobs.start()
            try:
                yield
            finally:
                await jobs.stop()

    app.router.lifespan_context=lifespan
