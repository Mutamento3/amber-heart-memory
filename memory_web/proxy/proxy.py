"""Hosted-history chat proxy with independent, filtered reply archiving.

Only the upstream credentials configured by the operator are sent upstream.
Frontend Authorization is consumed locally; no incoming headers are forwarded.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import re

import anyio
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import psycopg
from starlette.concurrency import run_in_threadpool

from memory_core.conversations.conversations import ConversationStore, ConversationConflict, DuplicateRequest, content_parts
from ..admin.access_keys import InvalidAccessKey, KeyConfigurationError
from .upstream_errors import error_message, response_error
from .hosted_context import HostedContext
from .usage_receipt import StreamReceipt, clean_response
from ..jobs.summary_jobs import SummaryJobs
from ..jobs.image_jobs import ImageJobs

LOG = logging.getLogger('memory_web.proxy')
MAX_BODY = 16 * 1024 * 1024
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EVENT = 4 * 1024 * 1024
MAX_PENDING_EVENTS = 4096
BOUNDARY = re.compile(br'\r\n\r\n|\n\n|\r\r')


# Kelivo's automatic title request; the self-hosted gateway never stores it as chat.
CLIENT_TITLE_PREFIXES = ('I will give you some dialogue content',)


def client_title_request(messages):
    last = messages[-1]
    if last.get('role') != 'user':
        return False
    content = last.get('content')
    if isinstance(content, list):
        content = ''.join(p.get('text') or '' for p in content if p.get('type') == 'text')
    return isinstance(content, str) and content.strip().startswith(CLIENT_TITLE_PREFIXES)


def error_response(status, code, message, **details):
    return JSONResponse({'error': {'message': message, 'type': 'proxy_error', 'code': code, **details}}, status)


def reject_json_constant(value):
    raise ValueError('JSON 不允许非有限数值')


class SSEFrames:
    def __init__(self):
        self.pending = b''

    def feed(self, chunk):
        self.pending += chunk
        frames = []
        while match := BOUNDARY.search(self.pending):
            frames.append(self.pending[:match.end()])
            self.pending = self.pending[match.end():]
        if len(self.pending) > MAX_EVENT or any(len(f) > MAX_EVENT for f in frames):
            raise ValueError('上游流式事件过大')
        return frames


def event_data(frame):
    data = b'\n'.join(line[5:].lstrip(b' ') for line in frame.splitlines() if line.startswith(b'data:'))
    if not data:
        return None
    if data == b'[DONE]':
        return 'done'
    return json.loads(data)


def safe_frame(frame, secret=None):
    value = event_data(frame)
    if isinstance(value, dict) and (value.get('error') or any(
        c.get('finish_reason') == 'error' for c in value.get('choices', []) if isinstance(c, dict))):
        value = {'error': {'message': error_message(value, secret, '上游生成失败'), 'type': 'upstream_error', 'code': 'upstream_stream_error'},
                 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'error'}]}
        return b'data: ' + json.dumps(value, ensure_ascii=False).encode() + b'\n\n'
    return frame


class Accumulator:
    """Parse archive data independently; reasoning is forwarded but never saved."""
    def __init__(self):
        self.text = []
        self.tools = {}
        self.finish_reason = None
        self.usage = None
        self.response_id = None
        self.failed = False
        self.done = False

    def consume(self, value):
        if value == 'done':
            self.done = True
            return
        if not isinstance(value, dict):
            return
        if value.get('error'):
            self.failed = True
        if isinstance(value.get('id'), str):
            self.response_id = value['id'][:512]
        if isinstance(value.get('usage'), dict):
            self.usage = value['usage']
        for choice in value.get('choices', []):
            if not isinstance(choice, dict) or choice.get('index', 0) != 0:
                continue
            if choice.get('finish_reason') is not None:
                self.finish_reason = choice['finish_reason']
                if self.finish_reason == 'error':
                    self.failed = True
            delta = choice.get('delta', choice.get('message', {}))
            if not isinstance(delta, dict):
                continue
            content = delta.get('content')
            if isinstance(content, str):
                self.text.append(content)
            for index, call in enumerate(delta.get('tool_calls') or []):
                position = call.get('index', index)
                if not isinstance(position, int) or not 0 <= position < 128:
                    raise ValueError('工具序号无效')
                target = self.tools.setdefault(position, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if call.get('id'):
                    target['id'] = call['id']
                for key in ('name', 'arguments'):
                    part = call.get('function', {}).get(key)
                    if isinstance(part, str):
                        target['function'][key] += part

    def result(self, *, interrupted=False, error_code=None):
        complete = not interrupted and not error_code and not self.failed and self.finish_reason is not None
        state = 'complete' if complete else 'interrupted'
        # An upstream failure does not turn its partial output into a reply.
        if self.failed:
            state = 'upstream_failed'
        return dict(content=''.join(self.text),
                    tools=[t for _, t in sorted(self.tools.items()) if t['id']],
                    state=state, finish_reason=self.finish_reason, usage=self.usage,
                    response_id=self.response_id,
                    error_stage=None if complete else 'upstream_stream',
                    error_code=error_code or ('upstream_stream_error' if self.failed else None if complete else 'stream_incomplete'))


class StreamArchive:
    """A bounded queue separates forwarding from parsing and final SQL writes."""
    def __init__(self, service, store, record, lease):
        self.service, self.store, self.record, self.lease = service, store, record, lease
        # Reserve one slot for the terminal signal, even on overload.
        self.queue = asyncio.Queue(maxsize=MAX_PENDING_EVENTS + 1)
        self.size = 0
        self.closed = False
        self.saved = False
        self.receipt = StreamReceipt()
        self.task = asyncio.create_task(self.run())
        service.tasks.add(self.task)
        self.task.add_done_callback(service.tasks.discard)

    def put(self, frame):
        self.size += len(frame)
        if self.size > MAX_ARCHIVE or self.queue.qsize() >= MAX_PENDING_EVENTS:
            raise ValueError('本轮存档超过长度上限')
        self.queue.put_nowait(frame)

    def close(self, interrupted=False, code=None):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait((interrupted, code))

    async def run(self):
        data = Accumulator()
        parsing_error = None
        while True:
            item = await self.queue.get()
            if isinstance(item, tuple):
                interrupted, code = item
                break
            if parsing_error is None:
                try:
                    data.consume(event_data(item))
                except (ValueError, TypeError, AttributeError):
                    parsing_error = 'invalid_upstream_event'
        try:
            self.saved = await self.service.save_archive(
                self.store,
                self.record,
                reported=self.receipt.filter.reported,
                reported_none=self.receipt.filter.reported_none,
                **data.result(
                    interrupted=interrupted, error_code=code or parsing_error
                ),
            )
        except (psycopg.Error, ValueError, OSError) as exc:
            await self.service.archive_failed(self.store, self.record, exc)
        finally:
            await self.lease.close()


class ProxyStream(StreamingResponse):
    def __init__(self, source, response, client, archive):
        super().__init__(source, media_type='text/event-stream',
                         headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        self.upstream, self.client, self.archive = response, client, archive

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Disconnect cancellation must not skip closing upstream sockets or
            # signalling the independent archive task. Do not finish generation.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
                await self.upstream.aclose()
                await self.client.aclose()
                if self.archive:
                    self.archive.close(interrupted=True)


class RequestLease:
    """Renew a live HTTP step; tool-wait leases expire without a heartbeat."""
    def __init__(self, service, store, record):
        self.service, self.store, self.record = service, store, record
        self.stopping = asyncio.Event()
        self.upstream = None
        self.lost = False
        self.transferred = False
        self.task = asyncio.create_task(self.run())
        service.lease_tasks.add(self.task)
        self.task.add_done_callback(service.lease_tasks.discard)

    async def run(self):
        interval = min(30, self.service.settings.turn_timeout_seconds / 3)
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                valid = await run_in_threadpool(self.store.renew, self.record)
            except (psycopg.Error, ValueError, OSError) as exc:
                self.service.log_archive_error(self.record, exc)
                valid = False
            if not valid:
                # Finishing a tool step clears active_request_id before the
                # recorder stops this heartbeat. That is normal completion,
                # not an expired lease; do not abort its final DONE frame.
                try:
                    if await run_in_threadpool(self.store.request_finished, self.record):
                        return
                except (psycopg.Error, ValueError, OSError):
                    pass
                self.lost = True
                if self.upstream:
                    try:
                        await self.upstream.aclose()
                    except httpx.HTTPError:
                        pass
                return

    async def close(self):
        self.stopping.set()
        await self.task


class ProxyService:

    def __init__(
        self, settings, keys, *, transport=None, models=None, summary_models=None, image_models=None
    ):
        self.settings, self.keys, self.transport = settings, keys, transport
        self.tasks = set()
        self.lease_tasks = set()
        self.stopping = asyncio.Event()
        self.reaper = None
        self.context = HostedContext(settings, models)
        self.summaries = SummaryJobs(settings, summary_models)
        self.images = ImageJobs(settings,image_models,on_saved=self.summaries.schedule)

    async def save_archive(self, store, record, **values):
        saved = await run_in_threadpool(store.finish, record, **values)
        if saved:
            self.summaries.schedule(store.space_id, record["turn_id"])
        return saved

    @staticmethod
    def log_archive_error(record, exc):
        LOG.error('archive_failure request_id=%s turn_id=%s type=%s code=%s',
                  record['request_id'], record['turn_id'], type(exc).__name__, getattr(exc, 'sqlstate', None))

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks))
        if self.lease_tasks:
            await asyncio.gather(*list(self.lease_tasks))
        await self.images.drain()
        await self.summaries.drain()

    async def start(self):
        if self.settings.upstream_base_url:
            await run_in_threadpool(ConversationStore.expire_leases, self.settings.dsn)
            self.reaper = asyncio.create_task(self.sweep())

    async def stop(self):
        await self.images.stop()
        self.stopping.set()
        if self.reaper:
            await self.reaper

    async def sweep(self):
        interval = min(30, self.settings.turn_timeout_seconds / 3)
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await run_in_threadpool(ConversationStore.expire_leases, self.settings.dsn)
            except (psycopg.Error, ValueError, OSError) as exc:
                LOG.error('turn_timeout_cleanup_failed type=%s code=%s', type(exc).__name__, getattr(exc, 'sqlstate', None))

    async def archive_failed(self, store, record, exc):
        self.log_archive_error(record, exc)
        # Best effort repair; a database outage can also prevent this update.
        try:
            await self.save_archive(
                store,
                record,
                state="archive_failed",
                error_stage="archive",
                error_code="archive_write_failed",
            )
        except (psycopg.Error, ValueError, OSError):
            pass

    async def authenticate(self, request):
        if self.keys is None:
            return None, error_response(503, 'keys_unconfigured', '服务器尚未配置访问钥匙主密钥')
        auth = request.headers.get('authorization', '')
        try:
            return await run_in_threadpool(self.keys.authenticate_header, auth), None
        except InvalidAccessKey:
            return None, error_response(401, 'invalid_api_key', '访问钥匙无效或已作废')
        except KeyConfigurationError as exc:
            return None, error_response(503, 'key_configuration_error', str(exc))

    def client(self):
        return httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(120, connect=10),
                                 follow_redirects=False, trust_env=False)

    def build(self, client, path, body=None):
        return client.build_request('GET' if body is None else 'POST',
            self.settings.upstream_base_url.rstrip('/') + path,
            # JSONB does not preserve object key order. Canonical wire encoding
            # keeps first-generation and frozen-replay prefixes byte identical.
            content=None if body is None else json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode(),
            headers={'Authorization': 'Bearer ' + self.settings.upstream_api_key,
                     **({'Content-Type': 'application/json'} if body is not None else {}),
                     'Accept-Encoding': 'identity'})

    async def models(self, request: Request):
        _, error = await self.authenticate(request)
        if error:
            return error
        if not self.settings.upstream_base_url:
            return error_response(503, 'upstream_unconfigured', '服务器尚未配置上游')
        try:
            async with self.client() as client:
                response = await client.send(self.build(client, '/models'), stream=True)
                try:
                    if not response.is_success:
                        message = await response_error(response, self.settings.upstream_api_key)
                        return error_response(response.status_code if response.status_code >= 400 else 502, 'upstream_error', message)
                    await response.aread()
                    value = response.json()
                    if isinstance(value, dict) and value.get('error'):
                        return error_response(502, 'upstream_error', error_message(value, self.settings.upstream_api_key))
                    return JSONResponse(value)
                finally:
                    await response.aclose()
        except (httpx.HTTPError, ValueError) as exc:
            return error_response(502, 'upstream_error', error_message(str(exc), self.settings.upstream_api_key, '上游模型列表暂时不可用'))

    async def body(self, request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise ValueError('请求超过 16 MiB')
        try:
            body = json.loads(raw, parse_constant=reject_json_constant)
        except (ValueError, UnicodeError):
            raise ValueError('请求必须是有效 JSON') from None
        if not isinstance(body, dict):
            raise ValueError('请求必须是 JSON 对象')
        intent = body.pop('memory', {})
        if not isinstance(intent, dict) or set(intent) - {'request_id', 'operation', 'target_turn_id'}:
            raise ValueError('memory 请求标记无效')
        if intent.get('operation') not in (None, 'new', 'retry', 'regenerate', 'edit', 'non_chat'):
            raise ValueError('请求操作类型无效')
        if 'request_id' in intent and (not isinstance(intent['request_id'], str) or not 1 <= len(intent['request_id']) <= 128):
            raise ValueError('请求编号须为 1–128 个字符')
        if 'target_turn_id' in intent and (type(intent['target_turn_id']) is not int or intent['target_turn_id'] <= 0):
            raise ValueError('目标轮次编号无效')
        if not isinstance(body.get('model'), str) or not 1 <= len(body['model']) <= 256:
            raise ValueError('必须提供模型名')
        if type(body.get('stream', False)) is not bool:
            raise ValueError('stream 必须是布尔值')
        if type(body.get('n', 1)) is not int or body.get('n', 1) != 1:
            raise ValueError('首发仅支持一次生成一份回复')
        messages = body.get('messages')
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) or m.get('role') not in ('system','developer','user','assistant','tool') for m in messages):
            raise ValueError('messages 格式无效')
        for message in messages:
            if isinstance(message.get('content'), list) and any(not isinstance(p, dict) or p.get('type') not in ('text','image_url') for p in message['content']):
                raise ValueError('前端消息只接受文字或图片内容')
            content_parts(message.get('content'))
            if message['role'] == 'tool' and (not isinstance(message.get('tool_call_id'), str) or not 1 <= len(message['tool_call_id']) <= 256):
                raise ValueError('工具结果缺少有效调用编号')
            calls = message.get('tool_calls', [])
            if not isinstance(calls, list) or len(calls) > 128:
                raise ValueError('工具调用格式无效')
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get('id'), str) or not 1 <= len(call['id']) <= 256:
                    raise ValueError('工具调用缺少有效编号')
                function = call.get('function')
                if (call.get('type') != 'function' or not isinstance(function, dict)
                    or not isinstance(function.get('name'), str) or not 1 <= len(function['name']) <= 256
                    or not isinstance(function.get('arguments'), str)):
                    raise ValueError('工具调用格式无效')
        if intent.get('operation') is None and client_title_request(messages):
            # Forward untouched: no memory injection, no stored turn, no extraction.
            intent['operation'] = 'non_chat'
        return body, intent

    async def failed(self, store, record, code):
        if store:
            try:
                await self.save_archive(
                    store,
                    record,
                    state="upstream_failed",
                    error_stage="upstream",
                    error_code=code,
                )
            except (psycopg.Error, ValueError, OSError) as exc:
                await self.archive_failed(store, record, exc)

    async def chat(self, request: Request):
        space, error = await self.authenticate(request)
        if error:
            return error
        if not self.settings.upstream_base_url:
            return error_response(503, 'upstream_unconfigured', '服务器尚未配置上游')
        try:
            body, intent = await self.body(request)
        except ValueError as exc:
            return error_response(400, 'invalid_request', str(exc))
        store = record = None
        if intent.get('operation') != 'non_chat':
            try:
                store = await run_in_threadpool(ConversationStore, self.settings.dsn, space, lease_seconds=self.settings.turn_timeout_seconds)
                record = await run_in_threadpool(store.begin, body, intent)
                body = record.pop('upstream_body')
            except ConversationConflict as exc:
                return error_response(409, exc.code, str(exc), **(exc.record or {}))
            except ValueError as exc:
                return error_response(400, 'invalid_request', str(exc))
            except (psycopg.Error, OSError):
                return error_response(503, 'archive_unavailable', '用户消息未能保存，请稍后重试')
        lease = RequestLease(self, store, record) if store else None
        try:
            if store and record['step_index'] == 0:
                self.images.schedule(space,record,body['messages'][-1])
            if store and record['step_index'] == 0:
                try:
                    body = await self.context.prepare(store, record, body)
                except ConversationConflict as exc:
                    return error_response(409, exc.code, str(exc))
                except (psycopg.Error, OSError, ValueError) as exc:
                    await self.archive_failed(store, record, exc)
                    return error_response(503, 'history_unavailable', '历史或本轮上下文未能读取、保存，请稍后重试')
            response = await self.exchange(body, store, record, lease)
            if record:
                response.headers['X-Memory-Turn-ID'] = str(record['turn_id'])
                response.headers['X-Memory-Request-ID'] = str(record['request_id'])
            return response
        finally:
            if lease and not lease.transferred:
                with anyio.CancelScope(shield=True):
                    await lease.close()

    async def exchange(self, body, store, record, lease):
        client = self.client()
        try:
            response = await client.send(self.build(client, '/chat/completions', body), stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            await self.failed(store, record, 'upstream_connection_failed')
            return error_response(502, 'upstream_error', error_message(str(exc), self.settings.upstream_api_key, '无法连接上游'))
        if lease:
            lease.upstream = response
            if lease.lost:
                await response.aclose()
                await client.aclose()
                await self.save_archive(
                    store,
                    record,
                    state="interrupted",
                    error_stage="lease",
                    error_code="turn_timeout",
                )
                return error_response(409, 'turn_timeout', '本轮已超时或占用失效，请重新发送')
        if not response.is_success:
            try:
                message = await response_error(response, self.settings.upstream_api_key)
            except httpx.HTTPError as exc:
                message = error_message(str(exc), self.settings.upstream_api_key)
            finally:
                await response.aclose()
                await client.aclose()
            await self.failed(store, record, 'upstream_http_' + str(response.status_code))
            return error_response(response.status_code if response.status_code >= 400 else 502, 'upstream_error', message)
        if not body.get('stream', False):
            try:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_ARCHIVE:
                        raise ValueError('上游回复过大')
                value = json.loads(raw)
                reported = []
                reported_none = False
                if store and isinstance(value, dict):
                    value, reported, reported_none = clean_response(value)
                data = Accumulator()
                data.consume(value)
                if not isinstance(value, dict) or data.failed or data.finish_reason is None:
                    await self.failed(store, record, 'invalid_upstream_response')
                    return error_response(502, 'upstream_error', error_message(value, self.settings.upstream_api_key, '上游回复无效'))
                if store:
                    try:
                        await self.save_archive(
                            store,
                            record,
                            reported=reported,
                            reported_none=reported_none,
                            **data.result(),
                        )
                    except (psycopg.Error, OSError, ValueError) as exc:
                        await self.archive_failed(store, record, exc)
                return JSONResponse(value)
            except (httpx.HTTPError, httpx.StreamError, ValueError, TypeError, AttributeError) as exc:
                await self.failed(store, record, 'invalid_upstream_response')
                return error_response(502, 'upstream_error', error_message(str(exc), self.settings.upstream_api_key, '上游回复无效或未完整收到'))
            finally:
                await response.aclose()
                await client.aclose()
        if 'text/event-stream' not in response.headers.get('content-type', ''):
            await response.aclose()
            await client.aclose()
            await self.failed(store, record, 'invalid_stream_type')
            return error_response(502, 'upstream_error', '上游未返回流式回复')
        archive = StreamArchive(self, store, record, lease) if store else None
        if lease:
            lease.transferred = True

        async def forward():
            frames = SSEFrames()
            ended = False
            tool_completion = False
            received = 0
            try:
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if archive and received > MAX_ARCHIVE:
                        raise ValueError('本轮回复超过长度上限')
                    for frame in frames.feed(chunk):
                        frame = safe_frame(frame, self.settings.upstream_api_key)
                        data = event_data(frame)
                        if isinstance(data, dict) and any(c.get('finish_reason') == 'tool_calls' for c in data.get('choices', [])):
                            tool_completion = True
                        clean_frames = archive.receipt.frames(frame, data) if archive else [frame]
                        for clean in clean_frames:
                            if archive:
                                archive.put(clean)
                            if data != 'done' or not tool_completion:
                                yield clean
                        if archive:
                            if data == 'done' and tool_completion:
                                # The client can execute an instant tool as soon
                                # as DONE arrives. Commit the waiting state first.
                                archive.close()
                                await asyncio.shield(archive.task)
                                if not archive.saved:
                                    raise ValueError('工具轮次未能保存，请重试')
                                yield frame
                if frames.pending.strip():
                    raise ValueError('上游流式事件未完整收到')
                if archive:
                    for clean in archive.receipt.flush():
                        archive.put(clean)
                        yield clean
                ended = True
            except (httpx.HTTPError, httpx.StreamError, ValueError, TypeError, AttributeError) as exc:
                if archive:
                    archive.close(interrupted=True, code='stream_read_failed')
                message = '本轮已超时或占用失效，请重新发送' if lease and lease.lost else error_message(str(exc), self.settings.upstream_api_key, '上游流式回复中断')
                value = {'error': {'message': message, 'code': 'turn_timeout' if lease and lease.lost else 'stream_read_failed'},
                         'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'error'}]}
                yield b'data: ' + json.dumps(value, ensure_ascii=False).encode() + b'\n\ndata: [DONE]\n\n'
            finally:
                if archive:
                    archive.close(interrupted=not ended)

        return ProxyStream(forward(), response, client, archive)


def attach_proxy(app, settings, keys, *, models=None, summary_models=None, image_models=None):
    service = ProxyService(settings, keys, models=models, summary_models=summary_models, image_models=image_models)
    app.state.proxy = service
    app.add_api_route('/v1/models', service.models, methods=['GET'])
    app.add_api_route('/v1/chat/completions', service.chat, methods=['POST'])
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            await service.start()
            try:
                yield
            finally:
                await service.stop()
                await service.drain()

    app.router.lifespan_context = lifespan
