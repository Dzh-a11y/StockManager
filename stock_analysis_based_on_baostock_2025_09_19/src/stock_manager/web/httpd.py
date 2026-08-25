"""Threading HTTP server binding for the local Web application (P3-2)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from stock_manager.web.app import WebApp
from stock_manager.web.config import WebConfig


class _WebRequestHandler(BaseHTTPRequestHandler):
    app: WebApp

    server_version = "StockManagerLocal"
    protocol_version = "HTTP/1.1"

    def _route(self, method: str, body_bytes: bytes | None) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        status, content_type, body = self.app.route(
            method, parsed.path, query, body_bytes
        )
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server protocol
        self._route("GET", None)

    def do_POST(self) -> None:  # noqa: N802 - http.server protocol
        self._route("POST", self._read_body())

    def do_PUT(self) -> None:  # noqa: N802 - http.server protocol
        self._route("PUT", self._read_body())

    def do_DELETE(self) -> None:  # noqa: N802 - http.server protocol
        self._route("DELETE", self._read_body())

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except ValueError:
            return b""
        if length < 0:
            return b""
        return self.rfile.read(length)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args
        return


def create_server(config: WebConfig, app: WebApp | None = None) -> ThreadingHTTPServer:
    config.validate()
    resolved = app if app is not None else WebApp(config)
    handler = type("WebRequestHandler", (_WebRequestHandler,), {"app": resolved})
    return ThreadingHTTPServer((config.host, config.port), handler)


def run_web(config: WebConfig, app: WebApp | None = None) -> None:
    server = create_server(config, app)
    try:
        server.serve_forever()
    finally:
        server.server_close()
