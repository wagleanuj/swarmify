"""The fake Slack endpoint: a real HTTP server on localhost recording each webhook post."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeSlack:
    """`with FakeSlack() as slack:` serves at `slack.url`; `slack.posts` holds each JSON body.
    Set `status` to make it refuse, as Slack does with 404 for a revoked webhook."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length") or 0)
                fake.posts.append(json.loads(self.rfile.read(size)))
                self.send_response(fake.status)
                self.end_headers()
                self.wfile.write(b"ok" if fake.status == 200 else b"no_service")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/services/T0/B0/x"

    def texts(self) -> list[str]:
        return [p["text"] for p in self.posts]

    def __enter__(self) -> "FakeSlack":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
