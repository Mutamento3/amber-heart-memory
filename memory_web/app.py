"""Standalone admin app and hosted-history proxy, without private gateway imports."""

import hmac
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
import psycopg
from starlette.concurrency import run_in_threadpool

from memory_core.storage.db import connect
from memory_core.cards.cards import CardStore
from memory_core.lifecycle.archives import ArchiveStore
from memory_core.cards.drafts import DraftStore, ExtractionConfigurationChanged, MaterialExpired, StructureInProgress
from memory_core.lifecycle.topic_summaries import TopicSummaryStore
from memory_core.cards.extraction_contract import (
    ContractError,
    LAYERS,
    MAX_TOPICS,
    MODES,
    COGNITIVE_STATUSES,
    MENTION_ROLES,
    ENTITY_TYPES,
    ENTITY_SUBTYPES,
    RELATION_TYPES,
    iso_date,
)
from memory_core.cards.graph_store import AmbiguousEntity
from memory_core.storage.schema import (
    DAILY_EXTRACTION_VERSION,
    DELAYED_APPROVAL_VERSION,
    DIARY_VERSION,
    TOPIC_PRESET_VERSION,
    TOPIC_SUMMARY_VERSION,
    IMAGE_DESCRIPTION_VERSION,
    RECENT_NOTES_VERSION,
    IMPORT_VERSION,
)
from memory_core.storage.config import Settings as CoreSettings
from memory_core.daily.diaries import DiaryConflict, DiaryStore
from memory_core.models.http_models import HTTPRecallModels
from memory_core.cards.review import ReviewStore
from memory_core.storage.space import (
    Participant,
    RevisionConflict,
    SpaceConfiguration,
    topic_presets,
)
from .admin.auth import AuthenticationError, RateLimited, Sessions
from .admin.catalog import Catalog
from .admin.static_assets import StaticAssets
from .config import WebSettings
from .admin.access_keys import AccessKeys, KeyConfigurationError
from .proxy.proxy import attach_proxy
from .mcp.mcp import attach_mcp
from .jobs.daily_jobs import DailyJobs
from .models.extraction_model import ExtractionModel
from .admin.import_routes import attach_imports, is_import_upload

LOG = logging.getLogger("memory_web")
STATIC = Path(__file__).with_name("static")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Body):
    password: Annotated[StrictStr, Field(max_length=256)]
    remember: StrictBool = False


class Version(Body):
    expected_version: Annotated[StrictInt, Field(gt=0)]


class Edit(Version):
    expected_policy_revision: Annotated[StrictInt, Field(gt=0)]
    payload: dict


class Approve(Version):
    expected_policy_revision: Annotated[StrictInt, Field(gt=0)]
    request_key: Annotated[StrictStr, Field(min_length=1, max_length=128)]


class CardPatch(Body):
    title: Annotated[StrictStr, Field(min_length=1, max_length=300)]
    topic_ids: list[Annotated[StrictInt, Field(gt=0)]] = Field(min_length=1, max_length=3)
    is_milestone: StrictBool


class RestoreCard(Body):
    expected_archived_at: Annotated[StrictStr, Field(min_length=1, max_length=64)]


class Person(Body):
    name: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    aliases: list[StrictStr] = Field(default_factory=list, max_length=100)


class Configure(Body):
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    user: Person
    assistant: Person
    topic_template: Literal["lover", "general"] | None = None
    acquaintance_date: StrictStr | None = None


class ConfigurationPatch(Body):
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    acquaintance_date: StrictStr | None


class PersonaPatch(Body):
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    persona: Annotated[StrictStr, Field(max_length=100000)]


class CoreMemoryPatch(Body):
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    core_memory: Annotated[StrictStr, Field(max_length=5000)]


class AutoApprovalPatch(Body):
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    auto_approve_enabled: StrictBool


class DiaryPatch(Body):
    expected_revision: Annotated[StrictInt, Field(gt=0)]
    content: Annotated[StrictStr, Field(min_length=1, max_length=100000)]


PositiveID = Annotated[int, Field(gt=0)]
Page = Annotated[int, Query(ge=1, le=1000000)]
Size = Annotated[int, Query(ge=1, le=100)]
Before = Annotated[int | None, Query(ge=1)]


def create_app(
    settings=None,
    *,
    recall_models=None,
    summary_models=None,
    diary_models=None,
    daily_clock=None,
    extraction_models=None,
    topic_summary_models=None,
    image_models=None,
    receipt_judge_models=None,
    recent_note_models=None,
):
    settings = settings or WebSettings.from_env()
    sessions = Sessions(settings.dsn, settings.password_file)
    catalog = Catalog(settings.dsn)
    config = SpaceConfiguration(settings.config_dsn)
    # Verify the ordinary application role without requiring any existing space.
    from memory_core.storage.postgres import PostgresStore

    PostgresStore(settings.dsn, "__startup__")
    with connect(settings.dsn) as connection:
        installed = connection.execute(
            "SELECT version,settings FROM memory_core.installation"
        ).fetchone()
        version = installed["version"]
        if version < TOPIC_PRESET_VERSION:
            raise ValueError("请先升级 memory_core 到版本 9，以支持固定主题模板")
        if settings.upstream_base_url and version < DIARY_VERSION:
            raise ValueError("启用托管代理前请先升级 memory_core 到版本 13")
        if settings.image_description_model and version < IMAGE_DESCRIPTION_VERSION:
            raise ValueError('启用图片描述前请先升级 memory_core 到版本 19')
        if settings.recent_notes_tidy_model and version < RECENT_NOTES_VERSION:
            raise ValueError('启用秘书整理前请先升级 memory_core 到版本 20')
        if settings.topic_summary_model and version < TOPIC_SUMMARY_VERSION:
            raise ValueError('启用主题总结前请先升级 memory_core 到版本 18')
        if settings.extraction_model and version < DELAYED_APPROVAL_VERSION:
            raise ValueError("启用每日抽卡与到期审核前请先升级 memory_core 到版本 16")
        if settings.access_key_master and not connection.execute("SELECT to_regclass('memory_web.space_access_keys') AS table_name").fetchone()['table_name']:
            raise ValueError('启用访问钥匙前请先运行 memory_web.install 升级网页结构')
        if settings.access_key_master:
            nullable = connection.execute("SELECT is_nullable FROM information_schema.columns WHERE table_schema='memory_web' AND table_name='space_access_keys' AND column_name='ciphertext'").fetchone()
            if not nullable or nullable['is_nullable'] != 'YES':
                raise ValueError('请先运行 memory_web.install 升级作废钥匙清理结构')
    with connect(settings.dsn) as connection:
        connection.execute("SELECT digest FROM memory_web.admin_sessions LIMIT 0")
    embedder, model_name = None, "unconfigured"
    providers = None
    if settings.fixed_models_file:
        from memory_core.testing.fixed_models import FixedModels

        embedder = FixedModels.from_file(settings.fixed_models_file)
        model_name = "explicit-fixed-synthetic-v1"
    else:
        providers = HTTPRecallModels(
            settings.recall_models,
            installed["settings"]["dimensions"],
            main_url=settings.upstream_base_url,
            main_key=settings.upstream_api_key,
        )
        if providers.embedding.configured:
            embedder = providers
            model_name = providers.embedding.model
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    keys = AccessKeys(settings.dsn, settings.config_dsn, settings.access_key_master) if settings.access_key_master else None
    attach_proxy(
        app,
        settings,
        keys,
        models=recall_models or embedder or providers,
        summary_models=summary_models,
        image_models=image_models,
    )
    daily = DailyJobs(
        settings,
        CoreSettings(**installed["settings"]),
        model=diary_models,
        enabled=version >= DIARY_VERSION,
        clock=daily_clock,
        topic_summary_models=topic_summary_models,
        receipt_judge_models=receipt_judge_models,
        extraction_models=extraction_models
        or ExtractionModel(
            settings, fixed_models=embedder if settings.fixed_models_file else None
        ),
        embedder=embedder,
        embedding_model=model_name,
        extraction_enabled=installed["version"] >= DAILY_EXTRACTION_VERSION,
    )
    app.state.daily = daily
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def daily_lifespan(app):
        if providers is not None:
            # Fail before accepting requests or launching jobs; each later
            # embed call validates the returned vector again before any write.
            await run_in_threadpool(providers.verify_dimensions)
        async with original_lifespan(app):
            await daily.start()
            try:
                yield
            finally:
                await daily.stop()

    app.router.lifespan_context = daily_lifespan
    attach_mcp(app, settings, keys, models=recall_models or embedder or providers)

    @app.exception_handler(DiaryConflict)
    async def diary_conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(KeyConfigurationError)
    async def key_configuration_error(request, exc):
        return JSONResponse({'detail': str(exc), 'code': 'key_configuration_error'}, 503)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        try:
            if (
                request.headers.get("host", "").lower()
                != settings.origin.split("://", 1)[1].lower()
            ):
                return JSONResponse({"detail": "访问地址与配置来源不符"}, 400)
            path = request.url.path
            # Machine credentials are a separate boundary: no browser session,
            # Origin or CSRF requirement, and never administrator privileges.
            if path.startswith('/v1/') or path in ('/mcp', '/mcp/'):
                response = await call_next(request)
                response.headers['Cache-Control'] = 'no-store'
                response.headers['X-Content-Type-Options'] = 'nosniff'
                return response
            mutation = request.method not in ("GET", "HEAD", "OPTIONS")
            if mutation:
                if (
                    request.headers.get("origin") != settings.origin
                    or request.headers.get("sec-fetch-site") == "cross-site"
                ):
                    return JSONResponse(
                        {"detail": "请求来源不符，请从管理网页操作"}, 403
                    )
                upload = is_import_upload(request)
                if (
                    request.headers.get("content-type", "").split(";")[0]
                    != ("application/octet-stream" if upload else "application/json")
                ):
                    return JSONResponse({"detail": "上传需使用原文件，其余操作仅接受 JSON 请求"}, 415)
                size = 0
                chunks = []
                if not upload:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > 1024 * 1024:
                            return JSONResponse({"detail": "请求超过 1 MiB"}, 413)
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
            public = (
                path == "/login" or path == "/api/login" or path.startswith("/static/")
            )
            if not public:
                session = await run_in_threadpool(
                    sessions.lookup, request.cookies.get(settings.cookie, "")
                )
                if not session:
                    if path.startswith("/api/"):
                        return JSONResponse({"detail": "登录已失效，请重新登录"}, 401)
                    return RedirectResponse("/login", 303)
                request.state.session = session
                if mutation and not hmac.compare_digest(
                    request.headers.get("x-csrf-token", ""), session["csrf"]
                ):
                    return JSONResponse({"detail": "页面校验已失效，请重新登录"}, 403)
            response = await call_next(request)
        except (psycopg.Error, OSError, ValueError) as exc:
            LOG.error(
                "auth_storage_failure type=%s code=%s",
                type(exc).__name__,
                getattr(exc, "sqlstate", None),
            )
            response = JSONResponse({"detail": "登录存储暂时不可用，请稍后重试"}, 503)
        # Static files set their own caching; everything else is never stored.
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            }
        )
        return response

    @app.exception_handler(RevisionConflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc), "code": "revision_conflict"}, 409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(ContractError)
    async def invalid_structure(request, exc):
        code = str(exc)
        if code in (
            "unknown_or_cross_space_entity",
            "known_entity_type_conflict",
            "dangling_relation",
        ):
            LOG.warning("draft_structure_invalid method=%s path=%s code=%s",
                request.method, request.url.path, code)
            return JSONResponse({
                "detail": "这张草稿里的人物或事物对不上，暂时不能保存或收录。草稿和当前编辑仍保留，需要先修复自动整理结果。",
                "code": code,
            }, 400)
        return JSONResponse({"detail": code}, 400)

    @app.exception_handler(AmbiguousEntity)
    async def ambiguous_structure(request, exc):
        LOG.warning("draft_structure_invalid method=%s path=%s code=ambiguous_entity",
            request.method, request.url.path)
        return JSONResponse({
            "detail": "草稿里有同名的人物或事物，暂时分不清指的是谁，不能收录。草稿和当前编辑仍保留，需要先修复自动整理结果。",
            "code": "ambiguous_entity",
        }, 400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Never echo rejected passwords, private payloads or database diagnostics.
        return JSONResponse(
            {
                "detail": "请求字段或类型无效",
                "fields": [".".join(map(str, e["loc"])) for e in exc.errors()],
            },
            422,
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        incident = uuid4().hex[:12]
        LOG.error(
            "database_failure incident=%s method=%s path=%s code=%s",
            incident,
            request.method,
            request.url.path,
            exc.sqlstate,
        )
        return JSONResponse(
            {"detail": "数据库校验失败或暂时不可用", "incident": incident}, 503
        )

    @app.post("/api/login")
    def login(body: Login, request: Request):
        try:
            token, csrf, lifetime = sessions.login(body.password, body.remember)
        except RateLimited as exc:
            return JSONResponse(
                {"detail": str(exc)}, 429, headers={"Retry-After": "300"}
            )
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc)) from None
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            settings.cookie,
            token,
            httponly=True,
            secure=settings.secure,
            samesite="strict",
            path="/",
            max_age=lifetime if body.remember else None,
        )
        return response

    @app.get("/api/session")
    def session(request: Request):
        return request.state.session | {
            "fixed_models": bool(settings.fixed_models_file),
            "mcp_url": settings.origin.rstrip('/') + '/mcp',
        }

    @app.post("/api/logout")
    def logout(request: Request):
        sessions.revoke(request.cookies[settings.cookie])
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            settings.cookie,
            path="/",
            secure=settings.secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/contract")
    def contract():
        return dict(
            max_topics=MAX_TOPICS,
            experience_modes=MODES,
            decay_layers=LAYERS,
            cognitive_statuses=COGNITIVE_STATUSES,
            mention_roles=MENTION_ROLES,
            entity_types=ENTITY_TYPES,
            entity_subtypes=ENTITY_SUBTYPES,
            relation_types=RELATION_TYPES,
            topic_presets=topic_presets(),
        )

    @app.get("/api/spaces")
    def spaces():
        return catalog.spaces()

    def require_space(space):
        with connect(settings.dsn) as connection:
            read = connection.execute(
                "SELECT 1 FROM memory_core.space_policies WHERE bot_id=%s AND user_entity_id IS NOT NULL",
                (space,),
            ).fetchone()
        if not read:
            raise HTTPException(404, "空间不存在")

    from .admin.recent_note_routes import attach_recent_notes
    attach_recent_notes(app, settings, require_space, enabled=version>=RECENT_NOTES_VERSION, model=recent_note_models)
    attach_imports(app,settings,require_space,enabled=version>=IMPORT_VERSION,
        model=daily.extraction_models,clock=daily_clock,embedder=embedder,embedding_model=model_name)

    @app.get('/api/spaces/{space}/access-key')
    def access_key(space: str):
        require_space(space)
        return {'configured': keys is not None, **(keys.get(space) if keys else {'key': None})}

    def require_keys():
        if keys is None:
            raise HTTPException(503, '服务器尚未配置访问钥匙主密钥')

    @app.post('/api/spaces/{space}/access-key')
    def generate_key(space: str):
        require_space(space)
        require_keys()
        return {'configured': True, **keys.rotate(space)}

    @app.delete('/api/spaces/{space}/access-key')
    def revoke_key(space: str):
        require_space(space)
        require_keys()
        return {'configured': True, **keys.revoke(space)}

    @app.get("/api/spaces/{space}/configuration")
    def configuration(space: str):
        require_space(space)
        return catalog.configuration(space)

    @app.patch("/api/spaces/{space}/configuration")
    def update_configuration(space: str, body: ConfigurationPatch):
        require_space(space)
        config.set_acquaintance_date(
            space,
            expected_revision=body.expected_revision,
            acquaintance_date=body.acquaintance_date,
        )
        return catalog.configuration(space)

    @app.patch("/api/spaces/{space}/persona")
    def update_persona(space: str, body: PersonaPatch):
        require_space(space)
        config.set_persona(space, expected_revision=body.expected_revision, persona=body.persona)
        return catalog.configuration(space)

    @app.patch('/api/spaces/{space}/core-memory')
    def update_core_memory(space: str, body: CoreMemoryPatch):
        require_space(space)
        config.set_core_memory(space, expected_revision=body.expected_revision, core_memory=body.core_memory)
        return catalog.configuration(space)

    @app.patch("/api/spaces/{space}/auto-approval")
    def update_auto_approval(space: str, body: AutoApprovalPatch):
        require_space(space)
        config.set_auto_approval(
            space,
            expected_revision=body.expected_revision,
            enabled=body.auto_approve_enabled,
        )
        return catalog.configuration(space)

    @app.get("/api/spaces/{space}/conversations")
    def conversations(space: str, before: Before = None, size: Size = 20):
        require_space(space)
        return catalog.conversations(space, before, size)

    @app.get("/api/spaces/{space}/conversations/{turn_id}/recall")
    def conversation_recall(space: str, turn_id: PositiveID):
        require_space(space)
        return catalog.conversation_recall(space, turn_id)

    @app.get("/api/spaces/{space}/summaries")
    def summaries(space: str, page: Page = 1, size: Size = 20):
        require_space(space)
        return catalog.summaries(space, page, size)

    @app.get("/api/spaces/{space}/diaries")
    def diaries(space: str, page: Page = 1, size: Size = 20):
        require_space(space)
        return catalog.diaries(space, page, size)

    @app.get("/api/spaces/{space}/diaries/{diary_id}")
    def diary(space: str, diary_id: PositiveID):
        require_space(space)
        return catalog.diary(space, diary_id)

    @app.patch("/api/spaces/{space}/diaries/{diary_id}")
    def edit_diary(space: str, diary_id: PositiveID, body: DiaryPatch):
        require_space(space)
        return DiaryStore(settings.dsn, space).edit(
            diary_id, body.content, body.expected_revision
        )

    @app.get("/api/spaces/{space}/summaries/{summary_id}")
    def summary(space: str, summary_id: PositiveID):
        require_space(space)
        return catalog.summary(space, summary_id)

    @app.put("/api/spaces/{space}/participants")
    def participants(space: str, body: Configure):
        if body.expected_revision != 0:
            require_space(space)
        elif len(space) > 128 or not space.strip():
            raise ValueError("空间标识须为 1–128 个字符")
        with connect(settings.config_dsn) as connection:
            previous = connection.execute(
                "SELECT source_retention_days FROM memory_core.space_policies WHERE bot_id=%s",
                (space,),
            ).fetchone()
        if previous and "topic_template" in body.model_fields_set:
            raise ValueError("主题模板只能在创建空间时选择")
        metadata = (
            {"acquaintance_date": body.acquaintance_date}
            if "acquaintance_date" in body.model_fields_set
            else {}
        )
        config.configure(
            space,
            Participant(body.user.name, body.user.aliases),
            Participant(body.assistant.name, body.assistant.aliases),
            expected_revision=body.expected_revision,
            topic_template=body.topic_template,
            source_retention_days=(
                previous["source_retention_days"] if previous else None
            ),
            **metadata,
            single_space=True,
        )
        return catalog.configuration(space)

    @app.get("/api/spaces/{space}/overview")
    def overview(space: str):
        require_space(space)
        return catalog.overview(
            space,
            now=daily.clock(),
            topic_summary_configured=daily.topic_summary_enabled and daily.topic_summary_models.configured,
            extraction_configured=daily.extraction_enabled
            and daily.extraction_models.configured,
        )

    @app.get("/api/spaces/{space}/drafts")
    def drafts(
        space: str,
        status: Literal["pending", "processed", "rejected"] = "pending",
        page: Page = 1,
        size: Size = 20,
        all_items: bool = False,
    ):
        require_space(space)
        return catalog.drafts(space, status, page, None if all_items else size)

    @app.get("/api/spaces/{space}/drafts/{draft_id}")
    def draft(space: str, draft_id: PositiveID):
        require_space(space)
        return catalog.draft(space, draft_id)

    @app.put("/api/spaces/{space}/drafts/{draft_id}")
    def edit(space: str, draft_id: PositiveID, body: Edit):
        require_space(space)
        return DraftStore(settings.dsn, space).edit(
            draft_id,
            body.payload,
            expected_version=body.expected_version,
            expected_policy_revision=body.expected_policy_revision,
            clock=daily.clock,
        )

    @app.post("/api/spaces/{space}/drafts/{draft_id}/reject")
    def reject(space: str, draft_id: PositiveID, body: Version):
        require_space(space)
        return DraftStore(settings.dsn, space).reject(
            draft_id, expected_version=body.expected_version
        )

    @app.post("/api/spaces/{space}/drafts/{draft_id}/restructure", status_code=202)
    def restructure(space: str, draft_id: PositiveID, body: Version, background_tasks: BackgroundTasks):
        require_space(space)
        if not daily.extraction_models.configured:
            raise HTTPException(503, "请先配置抽卡模型，再重新整理")
        store = DraftStore(settings.dsn, space)
        target = store.structure_retry_target(draft_id, expected_version=body.expected_version)

        def run():
            def record(event):
                if event.get('code'):
                    LOG.warning('draft_restructure_model_failed space=%s draft=%s code=%s', space, draft_id, event['code'])

            try:
                store.restructure(target, structurer=daily.extraction_models.stage('structure',record), clock=daily.clock,
                    model_timeout=settings.extraction_timeout_seconds)
            except StructureInProgress:
                # The other caller will publish the new version for both to read.
                return
            except Exception as error:
                if isinstance(error, RevisionConflict) and not isinstance(error, ExtractionConfigurationChanged):
                    return
                code = ('structure:configuration_changed' if isinstance(error, ExtractionConfigurationChanged)
                        else 'structure:material_expired' if isinstance(error, MaterialExpired) else 'structure:unknown')
                LOG.warning('draft_restructure_failed space=%s draft=%s type=%s code=%s',
                    space,draft_id,type(error).__name__,code)
                store.structure_retry_failed(target,code)

        background_tasks.add_task(run)
        return {'version': target['version'], 'wait_seconds': 2 * settings.extraction_timeout_seconds + 30}

    @app.post("/api/spaces/{space}/drafts/{draft_id}/approve")
    def approve(space: str, draft_id: PositiveID, body: Approve):
        require_space(space)
        return ReviewStore(settings.dsn, space).approve(
            draft_id, **body.model_dump(), embedder=embedder, embedding_model=model_name
        )

    @app.get("/api/spaces/{space}/topic-summaries/{batch_id}")
    def topic_summary(space: str, batch_id: PositiveID):
        require_space(space)
        return TopicSummaryStore(settings.dsn,space).detail(batch_id)

    @app.post("/api/spaces/{space}/topic-summaries/{batch_id}/approve")
    def approve_summary(space: str, batch_id: PositiveID, body: Approve):
        require_space(space)
        return TopicSummaryStore(settings.dsn,space).approve(batch_id,**body.model_dump(),embedder=embedder,now=daily.clock())

    @app.post("/api/spaces/{space}/topic-summaries/{batch_id}/reject")
    def reject_summary(space: str, batch_id: PositiveID, body: Version):
        require_space(space)
        return TopicSummaryStore(settings.dsn,space).reject(batch_id,**body.model_dump(),now=daily.clock(),daily=daily.policy)

    @app.post("/api/spaces/{space}/topic-summaries/{batch_id}/revoke")
    def revoke_summary(space: str, batch_id: PositiveID, body: Version):
        require_space(space)
        return TopicSummaryStore(settings.dsn,space).revoke(batch_id,**body.model_dump(),now=daily.clock(),daily=daily.policy)

    @app.get("/api/spaces/{space}/cards")
    def cards(
        space: str,
        q: Annotated[str, Query(max_length=500)] = "",
        topic: PositiveID | None = None,
        kind: Literal["atomic", "topic_summary", "all"] = "all",
        archive: Literal["active", "archived", "all"] = "active",
        archive_month: Annotated[str, Query(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')] | None = None,
        since: str | None = None,
        until: str | None = None,
        page: Page = 1,
        size: Size = 20,
    ):
        require_space(space)
        iso_date(since)
        iso_date(until)
        if since and until and since > until:
            raise ValueError("开始日期不能晚于结束日期")
        return catalog.cards(
            space,
            query=q,
            archive=archive,
            archive_month=archive_month,
            topic=topic,
            kind=kind,
            since=since,
            until=until,
            page=page,
            size=size,
        )

    @app.get("/api/spaces/{space}/archive-months")
    def archive_months(space: str):
        require_space(space)
        return catalog.archive_months(space)

    @app.post("/api/spaces/{space}/cards/{card_id}/restore")
    def restore_card(space: str, card_id: PositiveID, body: RestoreCard):
        require_space(space)
        return ArchiveStore(settings.dsn, space).restore(card_id,
            expected_archived_at=body.expected_archived_at, now=daily.clock())

    @app.get("/api/spaces/{space}/cards/{card_id}")
    def card(space: str, card_id: PositiveID):
        require_space(space)
        return catalog.card(space, card_id)

    @app.patch("/api/spaces/{space}/cards/{card_id}")
    def edit_card(space: str, card_id: PositiveID, body: CardPatch):
        require_space(space)
        return CardStore(settings.dsn,space).edit(card_id,body.model_dump())

    @app.delete("/api/spaces/{space}/cards/{card_id}")
    def delete_card(space: str, card_id: PositiveID, body: Body):
        require_space(space)
        return CardStore(settings.dsn,space).delete(card_id)

    assets = StaticAssets(STATIC)

    @app.get("/static/{name:path}")
    def static_file(name: str, request: Request):
        return assets.static(request, name)

    @app.get("/login")
    @app.get("/")
    @app.get("/overview")
    @app.get("/review")
    @app.get("/memories")
    @app.get("/memos")
    @app.get("/diaries")
    @app.get("/summaries")
    @app.get("/core-memory")
    @app.get("/bookmarks")
    @app.get("/settings")
    @app.get("/conversations")
    @app.get("/imports")
    @app.get("/drafts")
    @app.get("/cards")
    @app.get("/participants")
    @app.get("/topics")
    def page(request: Request):
        return assets.page(request)

    return app
