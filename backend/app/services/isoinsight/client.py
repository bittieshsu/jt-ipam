"""ISOinsight 的 HTTP client：登入（GET／POST form／POST JSON）、Cookie／Token、讀租約。

每一次工作一個 `IsoClient`（一個 httpx client＝一個 Cookie Jar）：登入一次、讀一次、結束就丟。
Cookie／Token 只在記憶體裡，不寫資料庫、不跨來源共用。

規則（規格 §5、§6）：
- 方法與格式是明確設定，**沒有自動 fallback**：401／403／405／415 都停在這一步，不改用別的方法再送一次帳密
- 3xx 一律不跟隨（回報狀態與去掉 Query 的 Location）
- HTTP 200、拿到 Cookie、拿到 Token 都不算「連線成功」—— 要讀到租約、通過結構驗證才算（那是 job 的事）
- Cookie 模式用標準 Cookie Jar（Path、Secure、到期照規則），不寫死 Cookie 名稱
- Token 模式依設定的欄位路徑取非空字串、放進設定的 Header；缺值就失敗，不猜欄位
- 讀租約遇到 401：重新登入一次、再讀一次；再失敗就結束。403 是權限不足，不反覆登入
- 讀租約遇到網路逾時或 502／503／504：最多再試一次；429 不重試（交給排程延後）；登入不重送

出站連線走既有的 `GuardedTransport`（連線當下檢查位址；內網位址照既有政策）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.safe_http import (
    GuardedTransport,
    UnsafeOutboundURL,
    _aresolve,
    check_addrs,
    transport_detail,
)
from app.services.isoinsight import config as cfg
from app.services.isoinsight.errors import RETRYABLE_TRANSPORT, IsoError
from app.services.isoinsight.parser import decode_json

LOGIN_MAX_BYTES = 1024 * 1024
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


# ── httpx 的請求日誌會印出完整網址：GET 登入時那就是帳密 ──────────────────────────
#
# httpx 對每個請求都在 INFO 寫一行 `HTTP Request: GET <完整網址> ...`，jt-ipam-sync 的日誌等級就是 INFO。
# 登入網址的 Query 在這裡換成 `<redacted>`；其他整合的日誌不受影響（只比對登記過的登入路徑）。

_LOGIN_PATHS: set[str] = set()


class _RedactLoginQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            new = []
            changed = False
            for a in args:
                if isinstance(a, httpx.URL) and a.query and a.path in _LOGIN_PATHS:
                    new.append(cfg.redact_url(str(a)))
                    changed = True
                else:
                    new.append(a)
            if changed:
                record.args = tuple(new)
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _RedactLoginQuery) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_RedactLoginQuery())


# httpcore 的 DEBUG 追蹤會把**回應標頭**整串印出來（含 Set-Cookie 的值）；開除錯日誌時 Session 就進了日誌。
# 只遮 Cookie／Set-Cookie／Authorization 類標頭的值，對所有整合一體適用（這幾個標頭的值本來就不該進日誌）。
_HEADER_TUPLE = re.compile(
    rb"\((b['\"](?:set-cookie|cookie|authorization|proxy-authorization|x-auth-token)['\"]),\s*b(['\"]).*?\2\)",
    re.IGNORECASE)


class _RedactHeaderValues(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.msg
        if isinstance(msg, str) and ("ookie" in msg or "uthorization" in msg or "uth-token" in msg.lower()):
            record.msg = _HEADER_TUPLE.sub(lambda m: b"(" + m.group(1) + b", b'***')",
                                           msg.encode("utf-8", "surrogateescape")).decode("utf-8", "surrogateescape")
        return True


for _name in ("httpcore.http11", "httpcore.http2", "httpcore.connection", "httpcore.proxy"):
    _lg = logging.getLogger(_name)
    if not any(isinstance(f, _RedactHeaderValues) for f in _lg.filters):
        _lg.addFilter(_RedactHeaderValues())


@dataclass(slots=True)
class Settings:
    """一次工作需要的連線設定（密碼已解密，只活在這次工作的記憶體裡）。"""
    base_url: str
    login_path: str
    login_method: str              # GET／POST
    post_format: str               # form／json（POST 才用）
    username: str
    password: str
    username_param: str = "username"
    password_param: str = "password"  # noqa: S105 -- 參數名／標頭名，不是秘密
    auth_mode: str = "cookie"      # cookie／token
    token_path: str | None = None
    token_header: str = "Authorization"  # noqa: S105 -- 參數名／標頭名，不是秘密
    token_prefix: str = "Bearer"  # noqa: S105 -- 參數名／標頭名，不是秘密
    lease_path: str = cfg.DEFAULT_LEASE_PATH
    verify_tls: bool = True
    connect_timeout: float = 5.0
    request_timeout: float = 30.0
    max_bytes: int = DEFAULT_MAX_BYTES

    @classmethod
    def from_source(cls, src: Any) -> Settings:
        password = cfg.decrypt_password(src)
        if not password:
            raise IsoError("CONFIG_INVALID", "the source has no usable password", stage="config")
        return cls(
            base_url=src.base_url, login_path=src.login_path, login_method=src.login_method,
            post_format=src.post_format, username=src.username, password=password,
            username_param=src.username_param, password_param=src.password_param,
            auth_mode=src.auth_mode, token_path=src.token_path, token_header=src.token_header,
            token_prefix=src.token_prefix or "", lease_path=src.lease_path, verify_tls=src.verify_tls,
            connect_timeout=float(src.connect_timeout_seconds), request_timeout=float(src.request_timeout_seconds),
            max_bytes=int(src.max_response_mib) * 1024 * 1024)

    def secrets(self) -> list[str]:
        return [self.password]


@dataclass(slots=True)
class Fetched:
    status: int
    content_type: str | None
    body: bytes


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    v = value.strip()
    try:
        secs = float(v)
    except ValueError:
        try:
            when = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        secs = (when - datetime.now(UTC)).total_seconds()
    return max(1.0, min(secs, 86400.0))


def _is_tls_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_html(resp_headers: httpx.Headers, body: bytes) -> bool:
    media = (resp_headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    head = body[:256].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return media in ("text/html", "application/xhtml+xml") or head.startswith((b"<!doctype", b"<html"))


class IsoClient:
    """一次工作的連線。`trace` 是去識別的逐步記錄（給測試連線畫面與同步記錄用）。"""

    def __init__(self, settings: Settings, *, check: Any = check_addrs, deadline: float | None = None) -> None:
        self.s = settings
        self._check = check
        self._deadline = deadline               # event loop 時間；None＝只受各請求逾時限制
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self.trace: list[dict[str, Any]] = []
        self.logins = 0
        self._lease_path, self._lease_params = cfg.split_path(settings.lease_path)
        self._login_url = cfg.join_url(settings.base_url, settings.login_path)
        self._lease_url = cfg.join_url(settings.base_url, self._lease_path)
        _LOGIN_PATHS.add(httpx.URL(self._login_url).path)

    async def __aenter__(self) -> IsoClient:
        timeout = httpx.Timeout(connect=self.s.connect_timeout, read=self.s.request_timeout,
                                write=self.s.request_timeout, pool=self.s.connect_timeout)
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False,
            headers={"User-Agent": _user_agent()},
            transport=GuardedTransport(verify=self.s.verify_tls, http2=False, check=self._check))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._token = None

    # ── 共用 ──

    def _time_left(self) -> float:
        if self._deadline is None:
            return float("inf")
        return self._deadline - asyncio.get_running_loop().time()

    def _err(self, code: str, message: str, *, stage: str, **kw: Any) -> IsoError:
        return IsoError(code, cfg.scrub(message, self.s.secrets()), stage=stage, **kw)

    @staticmethod
    def _trace_path(url: str, params: Any, stage: str) -> str:
        """記錄用的路徑：登入的 Query（GET 登入時就是帳密）一律遮蔽；租約路徑的固定 Query 是管理員設的，照樣顯示。"""
        path = httpx.URL(url).path
        if not params:
            return path
        if stage == "login":
            return f"{path}?<redacted>"
        return f"{path}?{httpx.QueryParams(params)}"

    async def _precheck(self, url: str, stage: str) -> None:
        u = httpx.URL(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            addrs = await _aresolve(u.host, port)
            if not addrs:
                raise UnsafeOutboundURL(f"no usable address for {u.host}")
            self._check(u.host, addrs)
        except UnsafeOutboundURL as exc:
            raise self._err("OUTBOUND_BLOCKED", f"outbound connection refused by policy: {exc}",
                            stage=stage) from exc

    async def _send(self, method: str, url: str, *, stage: str, max_bytes: int, params: Any = None,
                    data: Any = None, json_body: Any = None, headers: dict[str, str] | None = None,
                    ) -> tuple[httpx.Response, bytes]:
        assert self._client is not None, "use `async with IsoClient(...)`"
        await self._precheck(url, stage)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        entry: dict[str, Any] = {"stage": stage, "method": method, "path": self._trace_path(url, params, stage)}
        self.trace.append(entry)
        req = self._client.build_request(method, url, params=params, data=data, json=json_body, headers=headers)
        try:
            resp = await self._client.send(req, stream=True)
            try:
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        entry.update(status=resp.status_code, error="RESPONSE_LIMIT_EXCEEDED")
                        raise self._err("RESPONSE_LIMIT_EXCEEDED",
                                        f"response larger than {max_bytes} bytes", stage=stage,
                                        http_status=resp.status_code, max=max_bytes)
                    chunks.append(chunk)
            finally:
                await resp.aclose()
        except IsoError:
            raise
        except UnsafeOutboundURL as exc:
            entry["error"] = "OUTBOUND_BLOCKED"
            raise self._err("OUTBOUND_BLOCKED", f"outbound connection refused by policy: {exc}",
                            stage=stage) from exc
        except httpx.ConnectTimeout as exc:
            entry["error"] = "CONNECT_TIMEOUT"
            raise self._err("CONNECT_TIMEOUT", f"connect timeout ({transport_detail(exc)})", stage=stage) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            entry["error"] = "READ_TIMEOUT"
            raise self._err("READ_TIMEOUT", f"read timeout ({transport_detail(exc)})", stage=stage) from exc
        except httpx.DecodingError as exc:
            entry["error"] = "INVALID_RESPONSE"
            raise self._err("INVALID_RESPONSE", f"could not decode the response ({transport_detail(exc)})",
                            stage=stage, kind="encoding") from exc
        except httpx.HTTPError as exc:
            code = "TLS_VERIFY_FAILED" if _is_tls_failure(exc) else "CONNECT_FAILED"
            entry["error"] = code
            raise self._err(code, transport_detail(exc), stage=stage) from exc
        finally:
            entry["elapsed_ms"] = int((loop.time() - t0) * 1000)
        entry["status"] = resp.status_code
        entry["content_type"] = (resp.headers.get("content-type") or "")[:80] or None
        entry["bytes"] = total
        return resp, b"".join(chunks)

    def _status_error(self, resp: httpx.Response, *, stage: str) -> IsoError | None:
        st = resp.status_code
        if 300 <= st < 400:
            loc = cfg.redact_url(resp.headers.get("location") or "")
            return self._err("UNEXPECTED_REDIRECT", f"HTTP {st} redirect is not followed", stage=stage,
                             http_status=st, location=loc)
        if st == 429:
            ra = _retry_after(resp.headers.get("retry-after"))
            return self._err("RATE_LIMITED", "HTTP 429 Too Many Requests", stage=stage, http_status=st,
                             retry_after=ra, seconds=int(ra) if ra else None)
        if st in (405, 415):
            return self._err("METHOD_UNSUPPORTED", f"HTTP {st}", stage=stage, http_status=st)
        if st >= 500:
            return self._err("SERVER_ERROR", f"HTTP {st}", stage=stage, http_status=st)
        return None

    # ── 登入 ──

    async def login(self) -> None:
        s = self.s
        creds = {s.username_param: s.username, s.password_param: s.password}
        accept = {"Accept": "application/json, text/plain;q=0.9, */*;q=0.5"}
        self.logins += 1
        if s.login_method == "GET":
            resp, body = await self._send("GET", self._login_url, stage="login", max_bytes=LOGIN_MAX_BYTES,
                                          params=creds, headers=accept)
        elif s.post_format == "json":
            resp, body = await self._send("POST", self._login_url, stage="login", max_bytes=LOGIN_MAX_BYTES,
                                          json_body=creds, headers=accept)
        else:
            resp, body = await self._send("POST", self._login_url, stage="login", max_bytes=LOGIN_MAX_BYTES,
                                          data=creds, headers=accept)
        entry = self.trace[-1]
        entry["login_method"] = s.login_method
        if s.login_method == "POST":
            entry["post_format"] = s.post_format
        st = resp.status_code
        if st in (401, 403):
            raise self._err("AUTH_FAILED", f"login rejected (HTTP {st})", stage="login", http_status=st)
        err = self._status_error(resp, stage="login")
        if err is not None:
            raise err
        if not 200 <= st < 300:
            raise self._err("HTTP_ERROR", f"login returned HTTP {st}", stage="login", http_status=st)
        assert self._client is not None
        jar = list(self._client.cookies.jar)
        entry["cookies"] = sorted({c.name for c in jar})
        entry["cookie_attrs"] = [{"name": c.name, "path": c.path, "secure": bool(c.secure),
                                  "expires": c.expires is not None} for c in jar][:20]
        html = _is_html(resp.headers, body)
        if s.auth_mode == "token":
            if html:
                raise self._err("AUTH_FLOW_UNSUPPORTED", "login returned an HTML page", stage="login",
                                http_status=st)
            try:
                data, _warn = decode_json(body, resp.headers.get("content-type"), stage="login")
            except IsoError as exc:
                raise self._err("AUTH_RESPONSE_UNSUPPORTED", f"login response is not JSON: {exc}",
                                stage="login", http_status=st) from exc
            token = cfg.extract_token(data, s.token_path or "")
            entry["token_found"] = token is not None
            if token is None:
                raise self._err("AUTH_RESPONSE_UNSUPPORTED",
                                "no non-empty string at the configured token path", stage="login",
                                http_status=st, path=s.token_path)
            self._token = token
            return
        if not jar:
            if html:
                raise self._err("AUTH_FLOW_UNSUPPORTED", "login returned an HTML page and no cookie",
                                stage="login", http_status=st)
            raise self._err("AUTH_RESPONSE_UNSUPPORTED", "login set no cookie (cookie session mode)",
                            stage="login", http_status=st)

    def _auth_headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.s.auth_mode == "token" and self._token:
            prefix = (self.s.token_prefix or "").strip()
            h[self.s.token_header] = f"{prefix} {self._token}" if prefix else self._token
        return h

    # ── 租約 ──

    async def fetch_leases(self) -> Fetched:
        auth_retries = 0
        net_retries = 0
        while True:
            try:
                resp, body = await self._send("GET", self._lease_url, stage="fetch", max_bytes=self.s.max_bytes,
                                              params=self._lease_params or None, headers=self._auth_headers())
            except IsoError as exc:
                if exc.spec_code in RETRYABLE_TRANSPORT and net_retries < 1 and self._time_left() > 1:
                    net_retries += 1
                    continue
                raise
            st = resp.status_code
            if st == 401:
                if auth_retries < 1 and self._time_left() > 1:
                    auth_retries += 1
                    assert self._client is not None
                    self._client.cookies.clear()
                    self._token = None
                    await self.login()
                    continue
                raise self._err("AUTH_FAILED", "lease request is still unauthorized after logging in",
                                stage="fetch", http_status=st)
            if st == 403:
                raise self._err("PERMISSION_DENIED", "HTTP 403 on the lease request", stage="fetch",
                                http_status=st)
            if st in (502, 503, 504) and net_retries < 1 and self._time_left() > 1:
                net_retries += 1
                continue
            err = self._status_error(resp, stage="fetch")
            if err is not None:
                raise err
            if not 200 <= st < 300:
                raise self._err("HTTP_ERROR", f"lease request returned HTTP {st}", stage="fetch", http_status=st)
            return Fetched(status=st, content_type=resp.headers.get("content-type"), body=body)


async def anonymous_probe(settings: Settings, *, check: Any = check_addrs) -> dict[str, Any]:
    """人工觸發的額外診斷：不登入直接讀租約。讀得到只代表「認證必要性尚未確認」，不是帳密正確的證據。"""
    cli = IsoClient(settings, check=check)
    async with cli:
        try:
            resp, body = await cli._send("GET", cli._lease_url, stage="anonymous", max_bytes=settings.max_bytes,
                                         params=cli._lease_params or None, headers={"Accept": "application/json"})
        except IsoError as exc:
            return {"status": None, "readable": False, "error_code": exc.spec_code, "trace": cli.trace}
    readable = False
    if 200 <= resp.status_code < 300:
        try:
            data, _w = decode_json(body, resp.headers.get("content-type"), stage="anonymous")
            readable = isinstance(data, dict) and isinstance(data.get("dhcp_lease"), list)
        except IsoError:
            readable = False
    return {"status": resp.status_code, "readable": readable, "error_code": None, "trace": cli.trace}


def _user_agent() -> str:
    try:
        from app.version import __version__
    except ImportError:          # pragma: no cover
        __version__ = "dev"
    return f"jt-ipam/{__version__} (ISOinsight integration)"
