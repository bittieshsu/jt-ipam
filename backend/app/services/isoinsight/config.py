"""ISOinsight 來源設定：驗證、密碼加解密、敏感資訊遮蔽。

路徑規則（規格 §4）：登入與租約路徑都只能是**同一個 Base URL 底下的相對路徑**，不能另填跨主機
的網址 —— 否則改一個欄位就能把帳密送到別的主機。協定相對（`//host/x`）、帶協定、`..`、反斜線、
片段（`#`）一律拒絕；登入路徑不可帶 Query（GET 登入時帳密才是 Query，混進固定參數無從遮蔽）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.security import decrypt_secret, encrypt_secret

DEFAULT_LOGIN_PATH = "/api/logon"
DEFAULT_LEASE_PATH = "/isosvc?act=DhcpLease"
DEFAULT_TIMEZONE = "Asia/Taipei"

_CTRL = re.compile(r"[\x00-\x20\x7f]")
_PARAM = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,64}$")
_TOKEN_SEG = re.compile(r"^[A-Za-z0-9_\-]+$")
_HEADER = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,64}$")
#: 不能拿來放 Token 的標頭：改寫連線框架或與 Cookie 模式混淆
_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection", "cookie",
                                "content-type", "te", "upgrade", "proxy-authorization"})


def normalize_base_url(raw: Any) -> str:
    """`https://192.0.2.10[/前綴]`；不可帶帳密、Query、片段。回傳去掉結尾斜線的寫法。"""
    s = str(raw or "").strip()
    if not s or _CTRL.search(s):
        raise ValueError("base_url is empty or contains whitespace/control characters")
    parts = urlsplit(s)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("base_url must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("base_url has no host")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("base_url must not contain credentials")
    if parts.query or parts.fragment or "?" in s or "#" in s:
        raise ValueError("base_url must not contain a query or fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("base_url has an invalid port") from exc
    if port is not None and not (0 < port < 65536):
        raise ValueError("base_url has an invalid port")
    path = parts.path.rstrip("/")
    if path and ("\\" in path or "/../" in f"{path}/" or "/./" in f"{path}/"):
        raise ValueError("base_url path is not allowed")
    return f"{parts.scheme.lower()}://{parts.netloc}{path}"


def normalize_path(raw: Any, *, allow_query: bool) -> str:
    """同主機的相對路徑（以 `/` 開頭）。租約路徑可以帶固定的 Query（`?act=DhcpLease`）。"""
    s = str(raw or "").strip()
    if not s.startswith("/") or s.startswith("//"):
        raise ValueError("path must start with a single /")
    if _CTRL.search(s) or "\\" in s or "#" in s or "://" in s:
        raise ValueError("path contains characters that are not allowed")
    path, _, query = s.partition("?")
    if query and not allow_query:
        raise ValueError("login path must not contain a query")
    segs = path.split("/")
    if ".." in segs or "." in segs:
        raise ValueError("path must not contain . or .. segments")
    if len(s) > 255:
        raise ValueError("path is too long")
    return s


def split_path(path: str) -> tuple[str, list[tuple[str, str]]]:
    """`/isosvc?act=DhcpLease` → (`/isosvc`, [("act", "DhcpLease")])；Query 交給 HTTP client 編碼。"""
    p, _, q = path.partition("?")
    return p, parse_qsl(q, keep_blank_values=True)


def join_url(base_url: str, path: str) -> str:
    """Base URL（可含前綴）＋相對路徑（不含 Query）。"""
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def check_param_name(name: Any) -> str:
    s = str(name or "").strip()
    if not _PARAM.match(s):
        raise ValueError("parameter names may only use letters, digits, _ . - [ ] (max 64)")
    return s


def check_token_path(path: Any) -> str:
    s = str(path or "").strip()
    segs = s.split(".")
    if not s or len(segs) > 10 or not all(_TOKEN_SEG.match(x) for x in segs):
        raise ValueError("token path must be dot-separated keys, e.g. data.token")
    return s


def extract_token(body: Any, path: str) -> str | None:
    """依**明確設定**的路徑取 Token：每一段是物件的鍵，數字段也可以是陣列索引。
    取到的必須是非空字串，否則 None（缺值即失敗；不猜其他欄位）。"""
    cur = body
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return None
    return cur if isinstance(cur, str) and cur.strip() else None


def check_header_name(name: Any) -> str:
    s = str(name or "").strip()
    if not _HEADER.match(s) or s.lower() in _FORBIDDEN_HEADERS:
        raise ValueError("not an allowed header name")
    return s


def check_token_prefix(prefix: Any) -> str:
    s = str(prefix or "").strip()
    if len(s) > 32 or _CTRL.search(s):
        raise ValueError("token prefix must be at most 32 printable characters without spaces")
    return s


def check_timezone(tz: Any) -> str:
    s = str(tz or "").strip()
    try:
        ZoneInfo(s)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"unknown time zone {s!r}") from exc
    return s


# ── 密碼 ─────────────────────────────────────────────────────────────────────

def _aad(source_id: Any) -> bytes:
    return f"isoinsight_source:{source_id}:password".encode()


def encrypt_password(source_id: Any, raw: str) -> tuple[bytes, bytes]:
    return encrypt_secret(raw, aad=_aad(source_id))


def decrypt_password(src: Any) -> str | None:
    if not src.password_enc or not src.password_nonce:
        return None
    return decrypt_secret(src.password_enc, src.password_nonce, aad=_aad(src.id)).decode("utf-8")


# ── 遮蔽 ─────────────────────────────────────────────────────────────────────

def redact_url(url: str) -> str:
    """顯示／記錄用的網址：去掉帳密、Query、片段（有 Query 就標 `?<redacted>`）。"""
    s = str(url or "")
    parts = urlsplit(s)
    had_query = bool(parts.query) or "?" in s
    if parts.scheme and parts.hostname:
        host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        base = f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path}"
    else:
        base = s.split("?", 1)[0].split("#", 1)[0]
    return base + ("?<redacted>" if had_query else "")


_SENSITIVE_LINE = re.compile(
    r"(?im)\b(set-cookie|cookie|authorization|proxy-authorization|x-auth-token)\s*[:=]\s*[^\r\n]*")
_SENSITIVE_KV = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|session|sid)=([^&\s;]+)")


def scrub(text: Any, secrets: Iterable[str | None]) -> str:
    """把一段要記錄或顯示的文字裡的秘密值換成 `***`（原文與 URL 編碼後的寫法都換）。"""
    out = str(text or "")
    for sec in secrets:
        if not sec:
            continue
        for form in {sec, quote_plus(sec), quote(sec, safe=""), quote(sec)}:
            if form:
                out = out.replace(form, "***")
    out = _SENSITIVE_LINE.sub(lambda m: f"{m.group(1)}: ***", out)
    return _SENSITIVE_KV.sub(lambda m: f"{m.group(1)}=***", out)
