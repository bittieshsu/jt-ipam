"""ISOinsight 整合測試用的本機 HTTP 伺服器（真的走 TCP，不是 monkeypatch）。

用法：
    srv = MockServer()
    srv.route("POST", "/api/logon", lambda req: (200, [("Set-Cookie", "SID=abc; Path=/")], b"{}"))
    srv.route("GET", "/isosvc", [resp1, resp2])     # 清單＝依序回（最後一個重複使用）
    ...
    srv.requests   # 每一個收到的請求：method / path / query / params / headers / cookies / body

回應可以是 (status, headers, body) 或 callable(req) → 同樣的 tuple；`delay` 秒數讓它慢慢回。
"""
from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Callable
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

Resp = tuple[int, list[tuple[str, str]], bytes]


def json_resp(obj: Any, status: int = 200, headers: list[tuple[str, str]] | None = None) -> Resp:
    return status, [("Content-Type", "application/json"), *(headers or [])], json.dumps(obj).encode()


def lease_body(rows: list[dict[str, Any]]) -> Resp:
    return json_resp({"dhcp_lease": rows})


class MockServer:
    def __init__(self, *, ssl_context: Any = None) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.delay: dict[tuple[str, str], float] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:   # 安靜
                return

            def _handle(self) -> None:
                parts = urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                ck = SimpleCookie()
                if self.headers.get("Cookie"):
                    ck.load(self.headers["Cookie"])
                req = {"method": self.command, "path": parts.path, "query": parts.query,
                       "params": parse_qs(parts.query, keep_blank_values=True),
                       "headers": {k.lower(): v for k, v in self.headers.items()},
                       "cookies": {k: m.value for k, m in ck.items()}, "body": body}
                owner.requests.append(req)
                key = (self.command, parts.path)
                spec = owner.routes.get(key)
                if spec is None:
                    status, headers, out = 404, [("Content-Type", "text/plain")], b"not found"
                else:
                    if isinstance(spec, list):
                        item = spec.pop(0) if len(spec) > 1 else spec[0]
                    else:
                        item = spec
                    status, headers, out = item(req) if callable(item) else item
                if key in owner.delay:
                    time.sleep(owner.delay[key])
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                try:
                    self.wfile.write(out)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PUT = _handle      # noqa: N815

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        if ssl_context is not None:
            if ssl_context.minimum_version < ssl.TLSVersion.TLSv1_2:
                ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2   # 不讓舊版 TLS 進來（CodeQL #49）
            self.httpd.socket = ssl_context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.scheme = "https" if ssl_context is not None else "http"
        self.url = f"{self.scheme}://127.0.0.1:{self.port}"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def route(self, method: str, path: str, spec: Resp | Callable[[dict[str, Any]], Resp] | list[Any]) -> None:
        self.routes[(method, path)] = spec

    def hits(self, method: str, path: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and r["path"] == path]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
