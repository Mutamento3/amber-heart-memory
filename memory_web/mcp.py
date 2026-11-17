"""Official MCP SDK transport, authenticated separately from admin sessions."""

from contextlib import asynccontextmanager
import logging

from mcp.server import Server
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, INTERNAL_ERROR, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError
import psycopg
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from memory_core.memory_query import MemoryQueryService, MemoryQueryStore
from memory_core.bookmarks import BookmarkStore
from memory_core.schema import RECENT_NOTES_VERSION
from .access_keys import InvalidAccessKey, KeyConfigurationError
from .hosted_context import MissingRecallModels
from .mcp_tools import MemoryArguments, memory_description, render_result
from .bookmark_tool import BookmarkArguments, description as bookmark_description, render_bookmarks

LOG = logging.getLogger('memory_web.mcp')


class SpaceAuthorization:
    def __init__(self, app, keys):
        self.app, self.keys = app, keys

    async def __call__(self, scope, receive, send):
        request = Request(scope)
        try:
            if self.keys is None:
                response = JSONResponse({'detail': '服务器尚未配置访问钥匙主密钥'}, 503)
            elif len(request.headers.getlist('authorization')) != 1:
                response = JSONResponse({'detail': '需要请求头 Authorization: Bearer <空间访问钥匙>'},
                                        401, headers={'WWW-Authenticate': 'Bearer'})
            else:
                space = await run_in_threadpool(self.keys.authenticate_header, request.headers['authorization'])
                scope.setdefault('state', {})['memory_space'] = space
                await self.app(scope, receive, send)
                return
        except InvalidAccessKey:
            response = JSONResponse({'detail': '访问钥匙无效或已作废'}, 401,
                                    headers={'WWW-Authenticate': 'Bearer'})
        except KeyConfigurationError:
            response = JSONResponse({'detail': '访问钥匙主密钥配置不匹配'}, 503)
        except (psycopg.Error, OSError) as exc:
            LOG.error('mcp_auth_storage_failure type=%s code=%s', type(exc).__name__, getattr(exc, 'sqlstate', None))
            response = JSONResponse({'detail': '访问钥匙校验暂不可用'}, 503)
        await response(scope, receive, send)


def attach_mcp(app, settings, keys, *, models=None):
    server = Server('amber-heart', version='5B')
    models = models or MissingRecallModels()

    def query_service():
        # SDK request context is local to this HTTP request, including when
        # tools/list and parallel calls use different space credentials.
        space = server.request_context.request.state.memory_space
        return MemoryQueryService(MemoryQueryStore(settings.dsn, space), models)

    @server.list_tools()
    async def list_tools():
        try:
            service = await run_in_threadpool(query_service)
            policy = await run_in_threadpool(service.store.policy)
        except Exception as exc:
            LOG.error('mcp_tools_failed type=%s code=%s', type(exc).__name__, getattr(exc, 'sqlstate', None))
            raise McpError(ErrorData(code=INTERNAL_ERROR, message='读取记忆空间配置暂时失败，请稍后重试')) from None
        definitions = [Tool(name='memory', title='查记忆', description=memory_description(policy),
                     inputSchema=MemoryArguments.model_json_schema(),
                     annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                                 idempotentHint=False, openWorldHint=False))]
        if service.store.schema_version >= RECENT_NOTES_VERSION:
            definitions.append(Tool(name='mark_moment', title='记书签', description=bookmark_description(policy),
                inputSchema=BookmarkArguments.model_json_schema(), annotations=ToolAnnotations(
                    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)))
        return definitions

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            if name=='mark_moment':
                args = BookmarkArguments.model_validate(arguments).model_dump(exclude_none=True)
                space = server.request_context.request.state.memory_space
                store = await run_in_threadpool(BookmarkStore, settings.dsn, space)
                result = await run_in_threadpool(store.execute, args)
                return CallToolResult(content=[TextContent(type='text', text=render_bookmarks(args['action'], result))],
                    structuredContent=result, isError=not result['ok'])
            if name != 'memory':
                raise ValueError('没有这个工具，当前提供 memory（查记忆）和 mark_moment（记书签）')
            args = MemoryArguments.model_validate(arguments).model_dump(exclude_none=True)
            service = await run_in_threadpool(query_service)
            result = await run_in_threadpool(service.execute, args)
            text, data = render_result(args['action'], result)
            return CallToolResult(content=[TextContent(type='text', text=text)], structuredContent=data)
        except ValidationError as exc:
            fields = sorted({str(error['loc'][0]) for error in exc.errors() if error['loc']})
            message = '工具参数无效，请核对类型、范围和必填项：' + '、'.join(fields)
        except ValueError as exc:
            message = str(exc)
        except Exception as exc:
            # Neither SDK exception text nor DB/provider errors can disclose
            # credentials or private row contents to a client or application log.
            LOG.error('mcp_query_failed tool=%s type=%s code=%s', name if name in ('memory','mark_moment') else 'unknown', type(exc).__name__, getattr(exc, 'sqlstate', None))
            message = '工具调用暂时失败，请稍后重试'
        return CallToolResult(content=[TextContent(type='text', text=message)], isError=True)

    manager = StreamableHTTPSessionManager(
        app=server, stateless=True, json_response=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.origin.split('://', 1)[1]], allowed_origins=[settings.origin]))
    endpoint = SpaceAuthorization(StreamableHTTPASGIApp(manager), keys)
    # The SDK owns initialization, protocol headers, JSON-RPC and HTTP methods.
    # Exact routes avoid a redirect or an accidental /mcp/mcp mount path.
    app.router.routes.extend([Route('/mcp', endpoint=endpoint), Route('/mcp/', endpoint=endpoint)])
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application), manager.run():
            yield

    app.router.lifespan_context = lifespan
