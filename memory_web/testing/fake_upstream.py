"""Loopback-only synthetic upstream for integration tests, never a runtime fallback."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


class FakeUpstream:
    def __init__(self):
        self.requests = []
        self.release = threading.Event()
        self.models_error = None
        self.reply = None
        self.stream_parts = None
        self.stream_finish = 'stop'
        self.tool_batches = None
        self.tool_models = {'synthetic-round'}
        self.round_reasoning = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def json(self, value, status=200):
                data = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                owner.requests.append({'path': self.path, 'authorization': self.headers.get('Authorization')})
                if owner.models_error:
                    self.json({'error': {'message': owner.models_error}}, 503)
                    return
                self.json({'object': 'list', 'data': [{'id': 'synthetic-model', 'object': 'model'}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': body})
                model = body['model']
                if model == 'synthetic-error':
                    self.json({'error': {'message': '合成额度不足：' + self.headers['Authorization']}}, 429)
                    return
                round_message = None
                round_finish = 'stop'
                if model in owner.tool_models:
                    completed = sum(m['role'] == 'tool' for m in body['messages'])
                    round_message = {'role': 'assistant', 'content': owner.reply if owner.reply is not None else '合成步骤' + str(completed)}
                    batches = owner.tool_batches if owner.tool_batches is not None else [
                        [{'id': 'call-' + str(i), 'type': 'function', 'function': {'name': 'weather', 'arguments': '{}'}}]
                        for i in range(2)]
                    stage = sum(m['role'] == 'assistant' and bool(m.get('tool_calls')) for m in body['messages'])
                    if stage < len(batches):
                        round_message['tool_calls'] = batches[stage]
                        round_finish = 'tool_calls'
                        if owner.round_reasoning is not None:
                            round_message['reasoning_content'] = owner.round_reasoning + str(stage)
                if not body.get('stream'):
                    self.json({'id': 'synthetic-response', 'object': 'chat.completion',
                        'choices': [{'index': 0, 'message': round_message or {'role': 'assistant',
                            'content': owner.reply if owner.reply is not None else '合成回答' + (str(len(owner.requests)) if model == 'synthetic-echo' else ''),
                            'reasoning_content': '不应存档的合成思考'}, 'finish_reason': round_finish}],
                        'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}})
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True

                def event(delta=None, finish=None, **extra):
                    return {'id': 'synthetic-stream', 'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}], **extra}

                def send(value):
                    data = b'data: ' + json.dumps(value, ensure_ascii=False).encode() + b'\r\n\r\n'
                    # Includes byte boundaries inside Unicode and SSE separators.
                    for start in range(0, len(data), 7):
                        self.wfile.write(data[start:start+7])
                        self.wfile.flush()
                try:
                    if owner.stream_parts is not None:
                        for part in owner.stream_parts:
                            send(event({'content': part}))
                        if owner.stream_finish is not None:
                            send(event(finish=owner.stream_finish))
                            send({'choices': [], 'usage': {'total_tokens': 13}})
                            self.wfile.write(b'data: [DONE]\r\n\r\n')
                            self.wfile.flush()
                        return
                    if round_message:
                        send(event(round_message))
                        send(event(finish=round_finish))
                        send({'choices': [], 'usage': {'total_tokens': 13}})
                        self.wfile.write(b'data: [DONE]\r\n\r\n')
                        self.wfile.flush()
                        return
                    self.wfile.write(b': synthetic heartbeat\r\n\r\n')
                    self.wfile.flush()
                    send(event({'role': 'assistant', 'content': '第一段'}))
                    if model == 'synthetic-slow':
                        owner.release.wait(5)
                    send(event({'reasoning_content': '不应存档的合成思考'}))
                    if model == 'synthetic-stream-error':
                        send(event(finish='error', error={'message': '合成流式供应商故障：' + self.headers['Authorization']}))
                        return
                    if model == 'synthetic-truncated':
                        self.wfile.write(b'data: {"unfinished":')
                        self.wfile.flush()
                        return
                    if model == 'synthetic-tools':
                        send(event({'tool_calls': [{'index': 0, 'id': 'call-synthetic', 'type': 'function', 'function': {'name': 'weather', 'arguments': '{"city":'}}]}))
                        send(event({'tool_calls': [{'index': 0, 'function': {'arguments': '"test"}'}}]}))
                        send(event(finish='tool_calls'))
                    else:
                        send(event({'content': '第二段'}))
                        send(event(finish='stop'))
                    send({'choices': [], 'usage': {'total_tokens': 13}})
                    self.wfile.write(b'data: [DONE]\r\n\r\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + '/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
