"""Technitium DNS Server 的 HTTP API 用戶端（使用者 2026-10-08：下一個要支援 Technitium，它有 DNS 與 DHCP）。

DNS（DNSServer 的 `technitium` 類型，services/dns/technitium.py）與 DHCP（獨立整合，services/technitium_dhcp.py）
共用這一份。實機 15.6 確認過的行為：

- 回應一律 HTTP 200，成敗看 `status`：`ok`（資料在 `response`）、`error`（`errorMessage`）、`invalid-token`
- token 放 POST 表單（`application/x-www-form-urlencoded`）：各版本都吃，而且**不會出現在網址** ——
  網址會進對方或反向代理的存取記錄，也可能被帶進錯誤訊息
- 權限不足是 `error` + `Access was denied.`；DNS 紀錄另有逐個 zone 的權限（新建的 zone 預設只給管理員群組）
- 轉址不跟：HTTP→HTTPS 的 301/302 會讓 POST 變 GET、token 掉了，對方只回「token 無效」，看不出原因

錯誤訊息只放狀態、端點與底層例外原文，絕不放 token。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from app.core.safe_http import UnsafeOutboundURL, safe_request, transport_detail
from app.core.ui_error import UiError

#: 單一回應的大小上限（大的 zone 或租約表）；超過就中止，不把整份讀進記憶體
MAX_BYTES = 64 * 1024 * 1024
TIMEOUT = 30.0


class TechnitiumError(UiError):
    """呼叫 Technitium API 失敗。`code` 給前端翻譯（errors.technitium_*），`params` 帶端點與原因。"""


def _strip_query(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


class TechnitiumClient:
    def __init__(self, *, api_url: str, token: str, verify_tls: bool = True, timeout: float = TIMEOUT) -> None:
        self.base = api_url.rstrip("/")
        self.token = token
        self.verify_tls = verify_tls
        self.timeout = timeout

    async def call(self, path: str, **params: Any) -> dict[str, Any]:
        """呼叫一支 API，回 `response` 的內容（沒有 `response` 時回整份，例如 session/get）。"""
        body = urlencode({"token": self.token, **{k: v for k, v in params.items() if v is not None}})
        try:
            resp = await safe_request(
                "POST", f"{self.base}{path}", content=body.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                timeout=self.timeout, verify=self.verify_tls, max_bytes=MAX_BYTES, follow_redirects=False)
        except UnsafeOutboundURL as exc:
            raise TechnitiumError(f"blocked by the outbound connection policy: {exc}", code="technitium_ssrf",
                                  endpoint=path, reason=str(exc)[:200]) from exc
        except httpx.HTTPError as exc:
            reason = transport_detail(exc)
            raise TechnitiumError(f"{path}: {reason}", code="technitium_transport", endpoint=path,
                                  reason=reason) from exc
        if resp.is_redirect:
            loc = _strip_query(str(resp.headers.get("location") or ""))
            raise TechnitiumError(f"{path}: redirected to {loc}", code="technitium_redirect", endpoint=path,
                                  status=resp.status_code, location=loc)
        if resp.status_code != 200:
            raise TechnitiumError(f"{path}: HTTP {resp.status_code}", code="technitium_http", endpoint=path,
                                  status=resp.status_code)
        try:
            data = resp.json()
        except ValueError as exc:
            raise TechnitiumError(f"{path}: the reply is not JSON (wrong URL or a login page in between?)",
                                  code="technitium_not_json", endpoint=path) from exc
        if not isinstance(data, dict):
            raise TechnitiumError(f"{path}: unexpected reply", code="technitium_not_json", endpoint=path)
        status = str(data.get("status") or "")
        if status == "ok":
            out = data.get("response")
            return out if isinstance(out, dict) else {k: v for k, v in data.items() if k != "token"}
        msg = str(data.get("errorMessage") or status or "unknown error")[:300]
        if status == "invalid-token":
            raise TechnitiumError(f"{path}: invalid token or session expired", code="technitium_invalid_token",
                                  endpoint=path)
        if "access was denied" in msg.lower():
            raise TechnitiumError(f"{path}: access was denied", code="technitium_denied", endpoint=path)
        raise TechnitiumError(f"{path}: {msg}", code="technitium_error", endpoint=path, reason=msg)

    async def session(self) -> dict[str, Any]:
        """版本、伺服器名稱、帳號與 DNS／DHCP 權限（測試連線用：講得出 token 讀得到什麼、權限是否給太多）。"""
        data = await self.call("/api/user/session/get")
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        perms = info.get("permissions") if isinstance(info.get("permissions"), dict) else {}

        def _p(section: str) -> dict[str, bool]:
            p = perms.get(section) if isinstance(perms.get(section), dict) else {}
            return {"view": bool(p.get("canView")), "modify": bool(p.get("canModify") or p.get("canDelete"))}
        return {"version": str(info.get("version") or ""), "server": str(info.get("dnsServerDomain") or ""),
                "user": str(data.get("username") or ""), "zones": _p("Zones"), "dhcp": _p("DhcpServer")}
