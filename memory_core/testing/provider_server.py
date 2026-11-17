"""Local fake HTTP provider for embedding, chat and Voyage-shaped rerank tests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


class ProviderServer:
    def __init__(self, dimensions=3):
        self.vector = [1.0] + [0.0] * (dimensions - 1)
        self.requests = []
        self.replies = {}
        self.hook = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(
                    dict(
                        path=self.path,
                        body=body,
                        authorization=self.headers.get("Authorization"),
                    )
                )
                if owner.hook:
                    status, value = owner.hook(self.path, body)
                elif self.path.endswith("/embeddings"):
                    status, value = 200, dict(
                        data=[dict(index=0, embedding=owner.vector)]
                    )
                elif self.path.endswith("/rerank"):
                    status, value = 200, dict(
                        data=[
                            dict(index=i, relevance_score=float(i))
                            for i in reversed(range(len(body["documents"])))
                        ]
                    )
                else:
                    status, value = 200, dict(
                        choices=[
                            dict(
                                message=dict(
                                    content=owner.replies.get(
                                        body["model"], "1,2,3,4,5"
                                    )
                                ),
                                finish_reason="stop",
                            )
                        ]
                    )
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.03), daemon=True
        )
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
