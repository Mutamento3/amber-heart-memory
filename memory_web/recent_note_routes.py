"""Administrator-only backend endpoints for the 5C management page."""

from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool

from memory_core.bookmarks import BookmarkStore
from memory_core.recent_notes import RecentNotesStore
from .recent_note_jobs import RecentNoteTidyJobs


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class NotesSave(Body):
    expected_version: Annotated[StrictInt, Field(ge=0)]
    sections: dict[StrictStr, StrictStr]
    tidy_job_id: Annotated[StrictStr, Field(min_length=1, max_length=64)] | None = None


def attach_recent_notes(app, settings, require_space, *, enabled, model=None):
    jobs = RecentNoteTidyJobs(settings, model)
    app.state.recent_note_tidy = jobs

    def store(space):
        require_space(space)
        return RecentNotesStore(settings.dsn, space)

    @app.get('/api/spaces/{space}/recent-notes')
    def read(space: str):
        return store(space).read(jobs.policy)

    @app.put('/api/spaces/{space}/recent-notes')
    def save(space: str, body: NotesSave):
        return store(space).save(body.sections, body.expected_version, tidy_job_id=body.tidy_job_id)

    @app.get('/api/spaces/{space}/bookmarks')
    def bookmarks(space: str):
        require_space(space)
        return BookmarkStore(settings.dsn, space).list(limit=None)

    @app.delete('/api/spaces/{space}/bookmarks/{identifier}')
    def withdraw(space: str, identifier: UUID, body: Body):
        require_space(space)
        result = BookmarkStore(settings.dsn, space).execute(dict(action='delete', ids=[str(identifier)]))
        if not result['ok']:
            raise HTTPException(409, '书签已经整理或已撤回，请刷新待整理列表')
        return result

    @app.get('/api/spaces/{space}/recent-notes/tidy')
    def latest(space: str):
        return dict(configured=jobs.model.configured, job=store(space).read(jobs.policy)['tidy_job'])

    @app.post('/api/spaces/{space}/recent-notes/tidy/start')
    async def start(space: str, body: Body):
        await run_in_threadpool(store, space)
        return await jobs.start(space)

    @app.post('/api/spaces/{space}/recent-notes/tidy/cancel')
    async def cancel(space: str, body: Body):
        await run_in_threadpool(store, space)
        return await jobs.cancel(space)

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        if enabled:
            await run_in_threadpool(jobs.recover)
        async with original(application):
            try:
                yield
            finally:
                await jobs.stop()

    app.router.lifespan_context = lifespan
