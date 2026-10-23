"""Bigstack CubeCOS／OpenStack 同步（唯讀，Beta；0229）。

連線流程（Keystone v3）：
1. `POST /v3/auth/tokens`（唯一的 POST）：密碼（使用者＋網域，以 project 名稱＋網域取得 project scope）或
   Application Credential（id＋secret，本身已有範圍，不另外帶 scope）。token 在 `X-Subject-Token`，
   Service Catalog 在回應本體。
2. 依服務類型（identity／compute／network）＋region＋interface 從目錄挑端點；region 沒指定就用目錄裡第一個看到的。
   版本字尾不重複接：Neutron 的目錄網址可能有也可能沒有 `/v2.0`，Nova 的可能帶 project id。
3. Nova 先讀版本文件拿最大 microversion，再協商一個版本（見 `choose_microversion`）。
4. 之後所有請求都是 GET，帶 `X-Auth-Token`。token 剩不到 5 分鐘（或壽命一半，取小）先換；收到 401 只重新認證
   一次、只重試一次；不跟轉址（帶著 token 的請求被轉到別處，token 就送給別人了）。

刪除判定（與其他整合「完整才清」同一條規則，project_stale_integration_data）：
- 一份清單只要有任何一頁失敗、逾時、403，就是「不完整」：不刪、不累計 missing_syncs。
- Nova 的 all_tenants 被拒、退回 token 所屬 project 時，VM 清單的範圍縮小了 —— 也算不完整（別的 project 的 VM
  不可以當成被刪掉）。使用者自己關掉「跨 project」就不算縮小。
- port／子網路要「連續兩次完整同步」都沒出現才刪；VM 跟其他虛擬化整合一樣，一次完整同步沒出現就刪。

IPAM 連結（跟 Proxmox／Hyper-V 用同一個 `proxmox._link_ip_to_ipam`，來源換成 `openstack`）：
- 只有 VM 的 port（device_owner `compute:*`）的 fixed IP，而且那個 Neutron 子網路**已對應**到 jt-ipam 子網路，
  比對範圍就限定在那一個子網路 —— 不同 project 的重疊網段不會互相合併，也不靠 IP／MAC 去重。
- Floating IP 只存在 port 上，不是客體網卡的 IP，不連 IPAM。
- VM 關機不釋放 port 的 IP（Neutron 的配置仍在、也不會發給別人），所以跟 Proxmox「只有執行中的 VM 認領 IP」不同：
  port 在，就照樣連。

CubeCOS（product='cubecos'）：`GET {cubecos_url}/api/v1/datacenters`（不需認證）拿資料中心名稱，再
`GET /api/v1/datacenters/{dc}/nodes` 帶 Keystone token（CubeCOS API 接受 Keystone token：cube-cos-api
internal/runtime/router.go 的 isValidOpenstackToken）。只送 GET。401、轉址、HTML 一律是 cubecos_auth_failed；
CubeCOS 失敗不影響 OpenStack 的部分（記在能力裡、保留上一次的節點）。

不記錄 token、密碼與原始回應；錯誤訊息只帶狀態碼與傳輸層原因。
"""

from __future__ import annotations

import ipaddress
import re
import ssl
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.core.sqlin import in_values
from app.core.ui_error import UiError
from app.models.openstack import OpenStackInstance, OpenStackPort, OpenStackSubnet

# 想要的 Nova microversion 上限。理由：
# - 2.47 起 server 直接帶 flavor 的 vcpus／ram／disk（不必每個 flavor 另外 GET；flavor 被刪掉也還看得到）
# - 2.69 起 cell 掛掉時，那些 server 以「部分記錄」（只有 id、status=UNKNOWN）出現在清單裡，而不是直接不見 ——
#   不見的話會被當成刪除，這是刪除判定要的
# - 2.79 之前的版本沒有拿掉我們用到的欄位（OS-EXT-SRV-ATTR:hypervisor_hostname、tenant_id、status）；
#   再往上只是新增欄位，沒有必要追最新
# 伺服器的最大版本比這個低時就用伺服器的；版本文件讀不到時不帶標頭（＝2.1）。
WANTED_MICROVERSION = (2, 79)

_REFRESH_MARGIN = timedelta(minutes=5)
_PAGE_SIZE = 1000
_MAX_PAGES = 10_000
_MAX_BODY = 64 * 1024 * 1024
_TEST_TIMEOUT = 10.0
_SYNC_TIMEOUT = 60.0


class OpenStackError(UiError):
    """對外可讀的錯誤：code 是 openstack_* / cubecos_*，params 至少帶 reason（project_transport_error_detail）。"""

    def __init__(self, message: str, *, code: str, **params: Any) -> None:
        params.setdefault("reason", message)
        super().__init__(message, code=code, **params)


class OpenStackForbidden(OpenStackError):
    pass


# ────────────────────────── 憑證 ──────────────────────────

def _aad(instance_id: Any) -> bytes:
    return f"openstack_instance:{instance_id}:secret".encode()


def encrypt_secret_for(instance_id: Any, raw: str) -> tuple[bytes, bytes]:
    return encrypt_secret(raw, aad=_aad(instance_id))


def decrypt_secret_of(inst: OpenStackInstance) -> str:
    return decrypt_secret(inst.secret_enc, inst.secret_nonce, aad=_aad(inst.id)).decode("utf-8")


# ────────────────────────── 網址 ──────────────────────────

def _strip(url: str) -> str:
    return (url or "").strip().rstrip("/")


def identity_base(url: str) -> str:
    """Keystone 端點 → `.../v3`（有沒有 /v3 都接受）。"""
    u = _strip(url)
    if u.endswith("/v3"):
        u = u[:-3]
    return f"{u}/v3"


def tokens_url(auth_url: str) -> str:
    return f"{identity_base(auth_url)}/auth/tokens"


def neutron_base(url: str) -> str:
    """Neutron 目錄網址可能有也可能沒有 /v2.0：一律變成剛好一個。"""
    u = _strip(url)
    if u.endswith("/v2.0"):
        u = u[:-5]
    return f"{u}/v2.0"


_NOVA_VER = re.compile(r"/v2(?:\.1)?(?=/|$)")


def compute_base(url: str) -> str:
    """Nova 目錄網址原樣使用（可能帶 project id）；完全沒有版本段時補 /v2.1。"""
    u = _strip(url)
    return u if _NOVA_VER.search(urlsplit(u).path) else f"{u}/v2.1"


def compute_version_url(url: str) -> str:
    """版本文件在版本那一層（`.../v2.1/`）：舊式目錄網址後面的 project id 要拿掉。"""
    u = compute_base(url)
    sp = urlsplit(u)
    m = _NOVA_VER.search(sp.path)
    path = sp.path[:m.end()] if m else sp.path
    return urlunsplit((sp.scheme, sp.netloc, path + "/", "", ""))


def _ver(s: str | None) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def choose_microversion(server_max: str | None) -> str | None:
    v = _ver(server_max)
    if v is None or v < (2, 1):
        return None
    pick = min(v, WANTED_MICROVERSION)
    return f"{pick[0]}.{pick[1]}"


def cubecos_base(inst_cubecos_url: str | None, auth_url: str) -> str:
    """CubeCOS API 的根網址（可以帶路徑前綴，例如 https://cube/cos-api）；沒填就是 https://<Keystone 主機>。"""
    if inst_cubecos_url and inst_cubecos_url.strip():
        return _strip(inst_cubecos_url)
    host = urlsplit(auth_url).hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}"


# ────────────────────────── HTTP ──────────────────────────

def _verify_of(verify_tls: bool, ca_cert: str | None) -> bool | ssl.SSLContext:
    """有內部 CA 就只信任它（同 LDAP 的作法）；關掉驗證只在使用者明確選擇時。"""
    if not verify_tls:
        return False
    if ca_cert and ca_cert.strip():
        return ssl.create_default_context(cadata=ca_cert)
    return True


def _http_client(verify: bool | ssl.SSLContext, timeout: float) -> httpx.AsyncClient:
    """出站一律走 GuardedTransport（連線當下檢查位址）、不跟轉址、不信任環境變數。測試會換掉這一支。"""
    from app.core.safe_http import GuardedTransport
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                             transport=GuardedTransport(verify=verify))


def _transport_error(exc: BaseException, *, cubecos: bool = False) -> OpenStackError:
    from app.core.safe_http import ResponseTooLarge, UnsafeOutboundURL, transport_detail
    reason = transport_detail(exc)
    if isinstance(exc, ResponseTooLarge):
        return OpenStackError(f"response too large: {reason}",
                              code="cubecos_bad_response" if cubecos else "openstack_bad_response", reason=reason)
    if isinstance(exc, UnsafeOutboundURL):
        return OpenStackError(f"blocked: {exc}", code="cubecos_unreachable" if cubecos else "openstack_unreachable",
                              reason=str(exc)[:200])
    text_ = f"{reason} {exc.__cause__ or ''}"
    if not cubecos and re.search(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLError|SSL:", text_):
        return OpenStackError(f"TLS verification failed: {reason}", code="openstack_tls_failed", reason=reason)
    if cubecos and re.search(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed", text_):
        return OpenStackError(f"CubeCOS TLS verification failed: {reason}", code="cubecos_unreachable", reason=reason)
    return OpenStackError(f"connection failed: {reason}",
                          code="cubecos_unreachable" if cubecos else "openstack_unreachable", reason=reason)


@dataclass
class ConnConfig:
    auth_url: str
    auth_method: str = "password"
    username: str | None = None
    user_domain: str = "Default"
    project_name: str | None = None
    project_domain: str = "Default"
    app_credential_id: str | None = None
    secret: str = field(default="", repr=False)
    region: str | None = None
    interface: str = "public"
    verify_tls: bool = True
    ca_cert: str | None = field(default=None, repr=False)
    all_projects: bool = True
    product: str = "openstack"
    cubecos_url: str | None = None


def config_of(inst: OpenStackInstance) -> ConnConfig:
    return ConnConfig(
        auth_url=inst.auth_url, auth_method=inst.auth_method or "password", username=inst.username,
        user_domain=inst.user_domain or "Default", project_name=inst.project_name,
        project_domain=inst.project_domain or "Default", app_credential_id=inst.app_credential_id,
        secret=decrypt_secret_of(inst), region=inst.region, interface=inst.interface or "public",
        verify_tls=bool(inst.verify_tls), ca_cert=inst.ca_cert, all_projects=bool(inst.all_projects),
        product=inst.product or "openstack", cubecos_url=inst.cubecos_url)


class OpenStackClient:
    """一次測試或同步用的唯讀用戶端（`async with`）。"""

    def __init__(self, cfg: ConnConfig, *, timeout: float = _SYNC_TIMEOUT) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self.token: str | None = None
        self.expires_at: datetime | None = None
        self._lifetime: timedelta | None = None
        self.catalog: list[dict[str, Any]] = []
        self.region: str | None = cfg.region
        self.project_id: str | None = None
        self.microversion: str | None = None

    async def __aenter__(self) -> OpenStackClient:
        try:
            verify = _verify_of(self.cfg.verify_tls, self.cfg.ca_cert)
        except (ssl.SSLError, ValueError) as exc:
            raise OpenStackError(f"invalid CA certificate: {exc}", code="openstack_tls_failed",
                                 reason=f"CA: {exc}"[:200]) from exc
        self._http = _http_client(verify, self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── 傳輸 ──
    async def _send(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                    params: dict[str, Any] | None = None, json: Any = None, cubecos: bool = False) -> httpx.Response:
        from app.core.safe_http import safe_request
        assert self._http is not None, "use `async with OpenStackClient(...)`"
        try:
            return await safe_request(method, url, headers=headers, params=params, json=json,
                                      client=self._http, follow_redirects=False, max_bytes=_MAX_BODY,
                                      timeout=self.timeout)
        except OpenStackError:
            raise
        except Exception as exc:          # httpx 的連線／TLS／逾時、SSRF 擋下、回應過大
            raise _transport_error(exc, cubecos=cubecos) from exc

    # ── Keystone ──
    def _auth_body(self) -> dict[str, Any]:
        c = self.cfg
        if c.auth_method == "app_credential":
            return {"auth": {"identity": {"methods": ["application_credential"],
                                          "application_credential": {"id": c.app_credential_id or "",
                                                                     "secret": c.secret}}}}
        return {"auth": {
            "identity": {"methods": ["password"],
                         "password": {"user": {"name": c.username or "", "domain": {"name": c.user_domain or "Default"},
                                               "password": c.secret}}},
            "scope": {"project": {"name": c.project_name or "", "domain": {"name": c.project_domain or "Default"}}},
        }}

    async def authenticate(self) -> None:
        url = tokens_url(self.cfg.auth_url)
        resp = await self._send("POST", url, json=self._auth_body(),
                                headers={"Content-Type": "application/json", "Accept": "application/json"})
        if resp.status_code in (401,):
            raise OpenStackError("Keystone rejected the credentials (HTTP 401)", code="openstack_auth_failed",
                                 reason="HTTP 401")
        if resp.status_code == 403:
            raise OpenStackForbidden("Keystone refused the token request (HTTP 403)", code="openstack_forbidden",
                                     service="identity", reason="HTTP 403")
        if resp.status_code != 201 and resp.status_code != 200:
            raise OpenStackError(f"Keystone returned HTTP {resp.status_code}", code="openstack_bad_response",
                                 service="identity", reason=f"HTTP {resp.status_code}")
        token = resp.headers.get("X-Subject-Token")
        try:
            body = resp.json()["token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise OpenStackError("Keystone response is not a token", code="openstack_bad_response",
                                 service="identity", reason="no token in response") from exc
        if not token or not isinstance(body, dict):
            raise OpenStackError("Keystone response has no X-Subject-Token", code="openstack_bad_response",
                                 service="identity", reason="no X-Subject-Token")
        self.token = token
        now = datetime.now(UTC)
        try:
            self.expires_at = datetime.fromisoformat(str(body.get("expires_at")).replace("Z", "+00:00"))
        except ValueError:
            self.expires_at = now + timedelta(hours=1)
        if self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=UTC)
        self._lifetime = max(self.expires_at - now, timedelta(seconds=0))
        self.catalog = [s for s in (body.get("catalog") or []) if isinstance(s, dict)]
        self.project_id = (body.get("project") or {}).get("id")
        if not self.catalog:
            # 沒有 project scope 的 token 沒有目錄（例如 project 名稱填錯而 Keystone 退回 unscoped）
            raise OpenStackError("token has no service catalog", code="openstack_no_catalog_service",
                                 service="compute", reason="empty catalog")
        if self.region is None:
            self.region = self._first_region()
        for t in ("compute", "network"):
            self.endpoint(t)              # 必要的服務：缺了就是設定錯，現在就講

    def _first_region(self) -> str | None:
        for t in ("compute", "network", "identity"):
            for svc in self.catalog:
                if svc.get("type") != t:
                    continue
                for ep in svc.get("endpoints") or []:
                    if ep.get("interface") == self.cfg.interface:
                        return _text(ep.get("region_id") or ep.get("region"))
        return None

    def endpoint(self, service_type: str) -> str:
        for svc in self.catalog:
            if svc.get("type") != service_type:
                continue
            for ep in svc.get("endpoints") or []:
                if ep.get("interface") != self.cfg.interface:
                    continue
                if self.region and self.region not in (ep.get("region_id"), ep.get("region")):
                    continue
                url = str(ep.get("url") or "").strip()
                if url:
                    return url.rstrip("/")
        raise OpenStackError(
            f"no {service_type} endpoint for interface {self.cfg.interface!r} in region {self.region!r}",
            code="openstack_no_catalog_service", service=service_type,
            reason=f"{service_type}/{self.cfg.interface}/{self.region or '-'}")

    def _needs_refresh(self) -> bool:
        if self.token is None or self.expires_at is None:
            return True
        margin = _REFRESH_MARGIN
        if self._lifetime is not None:
            margin = min(margin, self._lifetime / 2)   # 壽命很短的 token：不要每個請求都重新認證
        return self.expires_at - datetime.now(UTC) < margin

    # ── 服務請求（只有 GET）──
    async def get(self, service: str, url: str, *, params: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None) -> httpx.Response:
        if self._needs_refresh():
            await self.authenticate()
        for attempt in (1, 2):
            h = {"Accept": "application/json", "X-Auth-Token": self.token or "", **(headers or {})}
            resp = await self._send("GET", url, params=params, headers=h)
            if resp.status_code == 401 and attempt == 1:
                await self.authenticate()     # token 被撤銷或提早失效：重新認證一次、重試一次
                continue
            return resp
        raise AssertionError("unreachable")

    async def get_json(self, service: str, url: str, *, params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None, allow_404: bool = False) -> dict[str, Any] | None:
        resp = await self.get(service, url, params=params, headers=headers)
        sc = resp.status_code
        if sc == 401:
            raise OpenStackError(f"{service}: token rejected (HTTP 401)", code="openstack_auth_failed",
                                 service=service, reason="HTTP 401")
        if sc == 403:
            raise OpenStackForbidden(f"{service}: forbidden (HTTP 403)", code="openstack_forbidden",
                                     service=service, reason="HTTP 403")
        if sc == 404 and allow_404:
            return None
        if 300 <= sc < 400:
            raise OpenStackError(f"{service}: unexpected redirect (HTTP {sc})", code="openstack_bad_response",
                                 service=service, reason=f"HTTP {sc} redirect")
        if sc != 200:
            raise OpenStackError(f"{service}: HTTP {sc}", code="openstack_bad_response", service=service,
                                 reason=f"HTTP {sc}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise OpenStackError(f"{service}: response is not JSON", code="openstack_bad_response",
                                 service=service, reason="not JSON") from exc
        if not isinstance(data, dict):
            raise OpenStackError(f"{service}: unexpected response", code="openstack_bad_response",
                                 service=service, reason="not a JSON object")
        return data

    async def list_all(self, service: str, url: str, key: str, *, params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None, page_size: int = _PAGE_SIZE) -> list[dict[str, Any]]:
        """`limit`＋`marker` 一頁一頁讀到底。任何一頁失敗就整個 raise（呼叫端當成「不完整」）。"""
        out: list[dict[str, Any]] = []
        marker: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            p = dict(params or {})
            p["limit"] = page_size
            if marker:
                p["marker"] = marker
            data = await self.get_json(service, url, params=p, headers=headers)
            assert data is not None
            items = data.get(key)
            if not isinstance(items, list):
                raise OpenStackError(f"{service}: no {key!r} list in response", code="openstack_bad_response",
                                     service=service, reason=f"missing {key}")
            out.extend(i for i in items if isinstance(i, dict))
            nxt = _next_marker(data.get(f"{key}_links"), items)
            if nxt is None:
                return out
            if nxt in seen:
                raise OpenStackError(f"{service}: pagination loop at {key}", code="openstack_bad_response",
                                     service=service, reason="pagination loop")
            seen.add(nxt)
            marker = nxt
        raise OpenStackError(f"{service}: too many pages of {key}", code="openstack_bad_response",
                             service=service, reason="too many pages")

    # ── Nova ──
    async def negotiate_compute(self) -> str | None:
        url = compute_version_url(self.endpoint("compute"))
        try:
            data = await self.get_json("compute", url, allow_404=True)
        except OpenStackError:
            data = None
        ver = (data or {}).get("version") if isinstance(data, dict) else None
        self.microversion = choose_microversion((ver or {}).get("version") if isinstance(ver, dict) else None)
        return self.microversion

    def compute_headers(self) -> dict[str, str]:
        if not self.microversion:
            return {}
        return {"OpenStack-API-Version": f"compute {self.microversion}",
                "X-OpenStack-Nova-API-Version": self.microversion}


def _next_marker(links: Any, items: list[Any]) -> str | None:
    if not isinstance(links, list):
        return None
    for ln in links:
        if isinstance(ln, dict) and ln.get("rel") == "next" and ln.get("href"):
            q = parse_qs(urlsplit(str(ln["href"])).query)
            if q.get("marker"):
                return str(q["marker"][-1])
            last = items[-1] if items else None
            return str(last.get("id")) if isinstance(last, dict) and last.get("id") else None
    return None


# ────────────────────────── 正規化 ──────────────────────────

_STATUS = {
    "ACTIVE": "running", "SHUTOFF": "stopped", "PAUSED": "paused", "SUSPENDED": "paused",
    "MIGRATING": "migrating", "RESIZE": "migrating", "VERIFY_RESIZE": "migrating",
}


def map_status(raw: Any) -> str:
    return _STATUS.get(str(raw or "").upper(), "unknown")


def norm_mac(raw: Any) -> str | None:
    from app.services.arp_evidence import normalize
    h = re.sub(r"[^0-9a-fA-F]", "", str(raw or ""))
    if len(h) != 12:
        return None
    return normalize(":".join(h[i:i + 2] for i in range(0, 12, 2)).lower())


def _text(v: Any) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _ip(v: Any) -> str | None:
    try:
        return str(ipaddress.ip_address(str(v).strip())) if v not in (None, "") else None
    except ValueError:
        return None


def normalize_server(raw: dict[str, Any], flavor: dict[str, Any] | None) -> dict[str, Any] | None:
    sid = _text(raw.get("id"))
    if not sid:
        return None
    partial = "name" not in raw          # 2.69+：cell 掛掉時的部分記錄
    fl = flavor or {}
    host_key = "OS-EXT-SRV-ATTR:hypervisor_hostname"
    disk = _int(fl.get("disk"))
    return {
        "id": sid[:64], "name": (_text(raw.get("name")) or sid)[:128], "partial": partial,
        "status": map_status(raw.get("status")),
        # 權限不夠時這個欄位整個不在：null，不猜（也不拿 OS-EXT-SRV-ATTR:host 或 hostId 硬配）
        "node": _text(raw.get(host_key)), "host_visible": host_key in raw,
        "project_id": _text(raw.get("tenant_id") or raw.get("project_id")),
        "vcpus": _int(fl.get("vcpus")), "memory_mb": _int(fl.get("ram")),
        # 0＝從 volume 開機或用映像檔大小：不是「0 GB」，當成不知道
        "disk_gb": disk or None,
    }


def normalize_port(raw: dict[str, Any]) -> dict[str, Any] | None:
    pid = _text(raw.get("id"))
    if not pid:
        return None
    fixed = []
    for f in raw.get("fixed_ips") or []:
        if isinstance(f, dict) and _ip(f.get("ip_address")):
            fixed.append({"subnet_id": _text(f.get("subnet_id")), "ip_address": _ip(f.get("ip_address"))})
    return {
        "port_id": pid[:64], "device_id": (_text(raw.get("device_id")) or "")[:64] or None,
        "device_owner": (_text(raw.get("device_owner")) or "")[:64] or None,
        "network_id": (_text(raw.get("network_id")) or "")[:64] or None,
        "project_id": (_text(raw.get("project_id") or raw.get("tenant_id")) or "")[:64] or None,
        "mac": norm_mac(raw.get("mac_address")), "fixed_ips": fixed,
        "status": (_text(raw.get("status")) or "")[:16] or None,
        "binding_host": _text(raw.get("binding:host_id")),
        "binding_visible": "binding:host_id" in raw,
    }


def normalize_subnet(raw: dict[str, Any]) -> dict[str, Any] | None:
    sid = _text(raw.get("id"))
    try:
        cidr = str(ipaddress.ip_network(str(raw.get("cidr")), strict=False))
    except ValueError:
        return None
    if not sid:
        return None
    return {
        "subnet_id": sid[:64], "network_id": (_text(raw.get("network_id")) or "")[:64] or None,
        "name": _text(raw.get("name")),
        "project_id": (_text(raw.get("project_id") or raw.get("tenant_id")) or "")[:64] or None,
        "cidr": cidr, "ip_version": _int(raw.get("ip_version")), "gateway_ip": _ip(raw.get("gateway_ip")),
        "allocation_pools": [p for p in (raw.get("allocation_pools") or []) if isinstance(p, dict)],
        "enable_dhcp": raw.get("enable_dhcp") if isinstance(raw.get("enable_dhcp"), bool) else None,
    }


def normalize_node(raw: dict[str, Any]) -> dict[str, Any]:
    mgmt = _text(raw.get("managementIP"))
    ip = _text(raw.get("ip"))
    return {
        "hostname": _text(raw.get("hostname")), "role": _text(raw.get("role")), "status": _text(raw.get("status")),
        "management_ip": mgmt,
        # CubeCOS v3.1.0：上線節點的 ip 就是管理 IP，只有離線節點才露出 provider IP —— 不同時才填
        "provider_ip": ip if ip and ip != mgmt else None,
        "storage_ip": _text(raw.get("storageIP")), "serial": _text(raw.get("serialNumber")),
    }


# ────────────────────────── 收集 ──────────────────────────

@dataclass
class Collected:
    servers: list[dict[str, Any]] | None = None
    servers_complete: bool = False
    #: 每一頁都抓完了（範圍縮小時也可能是 True：自己 project 的清單是完整的）
    servers_listed: bool = False
    #: 範圍縮小（想跨 project 被拒）時，這一輪看得到的是哪個 project：只在這個 project 裡判斷 VM 被刪
    scope_project_id: str | None = None
    all_projects: bool | None = None          # 這一輪實際有沒有跨 project
    ports: list[dict[str, Any]] | None = None
    ports_complete: bool = False
    subnets: list[dict[str, Any]] | None = None
    networks: dict[str, dict[str, Any]] | None = None
    floatingips: list[dict[str, Any]] | None = None
    projects: dict[str, str] = field(default_factory=dict)
    nodes: list[dict[str, Any]] | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return (self.servers_listed and self.ports_complete and self.subnets is not None
                and self.networks is not None and self.floatingips is not None)


def _failure(exc: OpenStackError, service: str) -> dict[str, Any]:
    """capabilities.services.<服務> 失敗時的欄位：畫面用 code 翻譯，params 只有 service 與 reason（契約）。"""
    reason = str(exc.params.get("reason") or exc)[:200]
    return {"ok": False, "code": exc.code, "params": {"service": service, "reason": reason},
            "message": str(exc)[:300], "reason": reason}


def _err_text(prefix: str, exc: OpenStackError) -> str:
    return f"{prefix}: [{exc.code}] {exc.params.get('reason') or exc}"[:300]


async def _collect_projects(cli: OpenStackClient) -> dict[str, str] | None:
    """project 名稱只是顯示用：讀不到不影響完整性。先試全部（管理員），403 就退回自己能進的。"""
    try:
        base = identity_base(cli.endpoint("identity"))
    except OpenStackError:
        base = identity_base(cli.cfg.auth_url)
    for path in ("/projects", "/auth/projects"):
        try:
            data = await cli.get_json("identity", f"{base}{path}")
        except OpenStackError:
            continue
        out = {}
        for p in (data or {}).get("projects") or []:
            if isinstance(p, dict) and p.get("id"):
                out[str(p["id"])] = str(p.get("name") or p["id"])
        return out
    return None


async def _collect_servers(cli: OpenStackClient, col: Collected) -> None:
    caps = col.capabilities
    compute = compute_base(cli.endpoint("compute"))
    await cli.negotiate_compute()
    caps["nova_microversion"] = cli.microversion
    hdr = cli.compute_headers()
    url = f"{compute}/servers/detail"
    params: dict[str, Any] = {}
    all_projects = False
    if cli.cfg.all_projects:
        try:
            await cli.get_json("compute", url, params={"all_tenants": 1, "limit": 1}, headers=hdr)
            params["all_tenants"] = 1
            all_projects = True
        except OpenStackForbidden:
            all_projects = False           # 政策不允許：退回 token 的 project（記在能力裡）
    col.all_projects = all_projects
    caps["all_projects"] = all_projects
    raw = await cli.list_all("compute", url, "servers", params=params, headers=hdr)
    # flavor：2.47 起內嵌；之前只有 id，逐一 GET（同一個只問一次；被刪掉的 flavor 回 404 → 不知道）
    flavors: dict[str, dict[str, Any] | None] = {}
    out = []
    for s in raw:
        fl = s.get("flavor") if isinstance(s.get("flavor"), dict) else None
        if fl is not None and "vcpus" not in fl and fl.get("id"):
            fid = str(fl["id"])
            if fid not in flavors:
                try:
                    data = await cli.get_json("compute", f"{compute}/flavors/{fid}", headers=hdr, allow_404=True)
                    flavors[fid] = (data or {}).get("flavor") if data else None
                except OpenStackError:
                    flavors[fid] = None
            fl = flavors[fid]
        n = normalize_server(s, fl)
        if n is not None:
            out.append(n)
    col.servers = out
    full = [s for s in out if not s["partial"]]
    caps["host_visible"] = any(s["host_visible"] for s in full) if full else None
    # 範圍縮小（想要跨 project 卻被拒）：清單本身完整，但不能拿來判斷「別的 project 的 VM 不見了」
    col.servers_listed = True
    col.servers_complete = all_projects or not cli.cfg.all_projects
    if not col.servers_complete:
        # 權限限制，不是同步失敗（10-22 DevStack：reader 角色 Nova 一律拒絕跨 project）：不寫進錯誤，
        # 否則 last_error 會讓「整合同步失敗」通知一直發；能力裡的 all_projects=false 已經說明了
        col.scope_project_id = cli.project_id


async def _collect_network(cli: OpenStackClient, col: Collected) -> None:
    caps = col.capabilities
    base = neutron_base(cli.endpoint("network"))
    try:
        data = await cli.get_json("network", f"{base}/extensions")
        caps["neutron_extensions"] = sorted(str(e.get("alias")) for e in (data or {}).get("extensions") or []
                                            if isinstance(e, dict) and e.get("alias"))
    except OpenStackError as exc:
        caps["neutron_extensions"] = None
        col.errors.append(_err_text("network extensions", exc))
    try:
        nets = await cli.list_all("network", f"{base}/networks", "networks")
        col.networks = {str(n["id"]): n for n in nets if n.get("id")}
    except OpenStackError as exc:
        col.errors.append(_err_text("networks", exc))
    try:
        subs = await cli.list_all("network", f"{base}/subnets", "subnets")
        col.subnets = [s for s in (normalize_subnet(x) for x in subs) if s]
    except OpenStackError as exc:
        col.errors.append(_err_text("subnets", exc))
    try:
        ports = await cli.list_all("network", f"{base}/ports", "ports")
        col.ports = [p for p in (normalize_port(x) for x in ports) if p]
        col.ports_complete = True
        caps["port_binding_visible"] = any(p["binding_visible"] for p in col.ports) if col.ports else None
    except OpenStackError as exc:
        col.errors.append(_err_text("ports", exc))
    try:
        col.floatingips = await cli.list_all("network", f"{base}/floatingips", "floatingips")
    except OpenStackError as exc:
        col.errors.append(_err_text("floating IPs", exc))


async def _collect_cubecos(cli: OpenStackClient, col: Collected) -> None:
    """CubeCOS 節點：失敗只記在能力裡，不影響 OpenStack 的部分。"""
    base = cubecos_base(cli.cfg.cubecos_url, cli.cfg.auth_url)
    cap: dict[str, Any] = {"ok": False, "url": base}
    col.capabilities.setdefault("services", {})["cubecos"] = cap
    try:
        dcs = await _cubecos_get(cli, f"{base}/api/v1/datacenters", auth=False)
        items = dcs.get("data")
        if not isinstance(items, list) or not items:
            raise OpenStackError("CubeCOS: no datacenter", code="cubecos_bad_response", reason="no datacenter")
        dc = next((d for d in items if isinstance(d, dict) and d.get("isLocal")), items[0])
        name = _text(dc.get("name")) if isinstance(dc, dict) else None
        if not name:
            raise OpenStackError("CubeCOS: datacenter has no name", code="cubecos_bad_response",
                                 reason="datacenter without name")
        cap["datacenter"] = name
        cap["version"] = _text(dc.get("version"))
        # 不帶分頁參數：CubeCOS 的節點分頁先切頁再排序、順序隨機，分頁讀會漏也會重複
        body = await _cubecos_get(cli, f"{base}/api/v1/datacenters/{name}/nodes", auth=True)
        data = body.get("data")
        nodes = data.get("nodes") if isinstance(data, dict) else data
        if not isinstance(nodes, list):
            raise OpenStackError("CubeCOS: no node list", code="cubecos_bad_response", reason="no nodes")
        col.nodes = [normalize_node(n) for n in nodes if isinstance(n, dict)]
        cap["ok"] = True
        cap["nodes"] = len(col.nodes)
    except OpenStackError as exc:
        if not (exc.code or "").startswith("cubecos_"):
            exc.code = "cubecos_unreachable"
        cap.update(_failure(exc, "cubecos"))
        col.errors.append(f"cubecos: [{cap['code']}] {cap['reason']}"[:300])


async def _cubecos_get(cli: OpenStackClient, url: str, *, auth: bool) -> dict[str, Any]:
    if auth and cli._needs_refresh():
        await cli.authenticate()
    headers = {"Accept": "application/json"}
    if auth:
        headers["X-Auth-Token"] = cli.token or ""
    resp = await cli._send("GET", url, headers=headers, cubecos=True)
    sc = resp.status_code
    ctype = (resp.headers.get("content-type") or "").lower()
    # 未認證：v3.1.0 回 401＋JSON（msg 是 SAML 登入網址）；前面有 nginx／proxy 時可能是 302 或 HTML 登入頁
    if sc in (401, 403) or 300 <= sc < 400:
        raise OpenStackError(f"CubeCOS rejected the request (HTTP {sc})", code="cubecos_auth_failed",
                             reason=f"HTTP {sc}")
    body_start = resp.content[:64].lstrip().lower()
    if "html" in ctype or body_start.startswith((b"<!doctype", b"<html")):
        raise OpenStackError("CubeCOS answered with a login page", code="cubecos_auth_failed",
                             reason=f"HTTP {sc} HTML")
    if sc != 200:
        raise OpenStackError(f"CubeCOS HTTP {sc}", code="cubecos_bad_response", reason=f"HTTP {sc}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise OpenStackError("CubeCOS response is not JSON", code="cubecos_bad_response", reason="not JSON") from exc
    if not isinstance(data, dict):
        raise OpenStackError("CubeCOS unexpected response", code="cubecos_bad_response", reason="not an object")
    return data


async def collect(inst: OpenStackInstance, *, timeout: float = _SYNC_TIMEOUT) -> Collected:
    """讀取所有資料。認證／目錄錯誤，或 Nova 與 Neutron 都讀不到時 raise；其他錯誤記在 errors（不完整）。"""
    col = Collected()
    caps = col.capabilities
    caps["checked_at"] = datetime.now(UTC).isoformat()
    cfg = config_of(inst)
    async with OpenStackClient(cfg, timeout=timeout) as cli:
        await cli.authenticate()
        caps["region"] = cli.region
        services: dict[str, Any] = {"identity": {"ok": True, "url": identity_base(cfg.auth_url)}}
        caps["services"] = services
        hard: list[OpenStackError] = []
        try:
            services["compute"] = {"ok": False, "url": cli.endpoint("compute")}
            await _collect_servers(cli, col)
            services["compute"]["ok"] = True
        except OpenStackError as exc:
            hard.append(exc)
            services["compute"].update(_failure(exc, "compute"))
            col.errors.append(_err_text("compute", exc))
        try:
            services["network"] = {"ok": False, "url": cli.endpoint("network")}
            await _collect_network(cli, col)
            services["network"]["ok"] = col.ports is not None
        except OpenStackError as exc:
            hard.append(exc)
            services["network"].update(_failure(exc, "network"))
            col.errors.append(_err_text("network", exc))
        if col.servers is None and col.ports is None:
            raise hard[0] if hard else OpenStackError("nothing could be read", code="openstack_bad_response",
                                                      reason="no data")
        projects = await _collect_projects(cli)
        col.projects = projects or {}
        caps["projects_visible"] = len(projects) if projects is not None else None
        if cfg.product == "cubecos":
            await _collect_cubecos(cli, col)
    return col


async def diagnose(inst: OpenStackInstance) -> dict[str, Any]:
    """測試連線（短逾時、同步回應）：各服務可不可達、跨 project、主機與 binding 欄位看不看得到。"""
    caps: dict[str, Any] = {"checked_at": datetime.now(UTC).isoformat(),
                            "services": {"identity": {"ok": False, "url": identity_base(inst.auth_url)}}}
    services = caps["services"]
    err: OpenStackError | None = None
    try:
        cfg = config_of(inst)
        async with OpenStackClient(cfg, timeout=_TEST_TIMEOUT) as cli:
            await cli.authenticate()
            services["identity"]["ok"] = True
            caps["region"] = cli.region
            # Nova：版本、跨 project、主機欄位（只拿一筆）
            services["compute"] = {"ok": False, "url": cli.endpoint("compute")}
            try:
                compute = compute_base(cli.endpoint("compute"))
                await cli.negotiate_compute()
                caps["nova_microversion"] = cli.microversion
                url, hdr = f"{compute}/servers/detail", cli.compute_headers()
                data = None
                caps["all_projects"] = False
                if cfg.all_projects:
                    try:
                        data = await cli.get_json("compute", url, params={"all_tenants": 1, "limit": 1}, headers=hdr)
                        caps["all_projects"] = True
                    except OpenStackForbidden:
                        data = None
                if data is None:
                    data = await cli.get_json("compute", url, params={"limit": 1}, headers=hdr)
                srv = [s for s in (data or {}).get("servers") or [] if isinstance(s, dict) and "name" in s]
                caps["host_visible"] = ("OS-EXT-SRV-ATTR:hypervisor_hostname" in srv[0]) if srv else None
                services["compute"]["ok"] = True
            except OpenStackError as exc:
                err = err or exc
                services["compute"].update(_failure(exc, "compute"))
            # Neutron：擴充、port binding（只拿一筆）
            services["network"] = {"ok": False, "url": cli.endpoint("network")}
            try:
                base = neutron_base(cli.endpoint("network"))
                ext = await cli.get_json("network", f"{base}/extensions")
                caps["neutron_extensions"] = sorted(str(e.get("alias")) for e in (ext or {}).get("extensions") or []
                                                    if isinstance(e, dict) and e.get("alias"))
                pdata = await cli.get_json("network", f"{base}/ports", params={"limit": 1})
                ports = [p for p in (pdata or {}).get("ports") or [] if isinstance(p, dict)]
                caps["port_binding_visible"] = ("binding:host_id" in ports[0]) if ports else None
                services["network"]["ok"] = True
            except OpenStackError as exc:
                err = err or exc
                services["network"].update(_failure(exc, "network"))
            projects = await _collect_projects(cli)
            caps["projects_visible"] = len(projects) if projects is not None else None
            if cfg.product == "cubecos":
                col = Collected(capabilities=caps)
                await _collect_cubecos(cli, col)
                cube = services["cubecos"]
                if not cube["ok"] and err is None:
                    err = OpenStackError(cube.get("reason") or "CubeCOS failed", code=cube["code"],
                                         reason=cube.get("reason") or "")
    except OpenStackError as exc:
        err = err or exc
        if not services["identity"]["ok"]:
            services["identity"].update(_failure(exc, "identity"))
    ok = err is None
    return {"ok": ok, "capabilities": caps,
            "error": None if err is None else {"code": err.code, "params": err.params, "message": str(err)}}


# ────────────────────────── 同步 ──────────────────────────

def _scope_ids(inst: OpenStackInstance) -> set[Any] | None:
    ids = set()
    for x in inst.scope_subnet_ids or []:
        try:
            ids.add(uuid.UUID(str(x)))
        except ValueError:
            continue
    return ids or None


async def _ensure_cluster(session: AsyncSession, inst: OpenStackInstance) -> Any:
    """一個連線對映一筆 virt_cluster（type openstack，名稱＝連線名稱）。不拿別的平台的叢集來用。"""
    from app.models.virt import VirtCluster

    cluster = await session.get(VirtCluster, inst.cluster_id) if inst.cluster_id else None
    if cluster is not None and cluster.type != "openstack":
        cluster = None
    if cluster is None:
        for cand in (inst.name, f"{inst.name} (OpenStack)"):
            cand = cand[:128]
            row = (await session.execute(select(VirtCluster).where(VirtCluster.name == cand).limit(1))).scalars().first()
            if row is None:
                cluster = VirtCluster(name=cand, type="openstack", description=inst.description)
                session.add(cluster)
                await session.flush()
                break
            owned = (await session.execute(select(OpenStackInstance.id).where(
                OpenStackInstance.cluster_id == row.id, OpenStackInstance.id != inst.id).limit(1))).first()
            if row.type == "openstack" and owned is None:
                cluster = row
                break
        if cluster is None:
            raise OpenStackError(f"cluster name {inst.name!r} is taken", code="openstack_name_taken",
                                 reason=f"cluster name {inst.name!r} taken")
    inst.cluster_id = cluster.id
    return cluster


async def _ipam_subnets_by_cidr(session: AsyncSession, cidrs: set[str],
                                scope: set[Any] | None) -> dict[str, list[Any]]:
    from app.models.subnet import Subnet
    if not cidrs:
        return {}
    stmt = select(Subnet.id, Subnet.cidr).where(Subnet.archived_at.is_(None), in_values(Subnet.cidr, sorted(cidrs)))
    if scope:
        stmt = stmt.where(in_values(Subnet.id, scope))
    out: dict[str, list[Any]] = {}
    for sid, cidr in (await session.execute(stmt)).all():
        out.setdefault(str(ipaddress.ip_network(str(cidr), strict=False)), []).append(sid)
    return out


async def sync_instance(session: AsyncSession, inst: OpenStackInstance) -> dict[str, Any]:
    """鏡像 VM、port、子網路；把已對應子網路裡的 VM 位址接進 IPAM。同一個連線同時只跑一個（advisory lock）。"""
    from app.models.virt import VirtualMachine, VMInterface
    from app.services.arp_precedence import MacRun
    from app.services.hostname_reports import HostnameRun, enabled_peers
    from app.services.ip_autocreate import addable_subnets
    from app.services.proxmox import _link_ip_to_ipam

    got = (await session.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                                 {"k": f"openstack_sync:{inst.id}"})).scalar()
    if not got:
        return {"skipped": "already_running"}
    try:
        col = await collect(inst)
    except OpenStackError as exc:
        inst.last_error = str(exc)[:2000]
        inst.last_sync_at = datetime.now(UTC)
        await session.commit()
        raise

    now = datetime.now(UTC)
    cluster = await _ensure_cluster(session, inst)
    scope = _scope_ids(inst)
    nets = col.networks or {}
    projects = col.projects
    summary: dict[str, Any] = {
        "cluster": cluster.name, "vms": len(col.servers or []), "ports": len(col.ports or []),
        "subnets": len(col.subnets or []), "removed_vms": 0, "removed_ports": 0, "removed_subnets": 0,
        "ipam_linked": 0, "pending_subnets": 0, "all_projects": col.all_projects,
        "complete": col.complete, "errors": list(col.errors),
    }

    # ── 子網路與對應 ──
    sub_rows = {r.subnet_id: r for r in (await session.execute(
        select(OpenStackSubnet).where(OpenStackSubnet.instance_id == inst.id))).scalars()}
    if col.subnets is not None:
        counts = Counter(s["cidr"] for s in col.subnets)
        cands = await _ipam_subnets_by_cidr(session, set(counts), scope)
        seen_sub: set[str] = set()
        for s in col.subnets:
            srow = sub_rows.get(s["subnet_id"])
            if srow is None:
                srow = OpenStackSubnet(instance_id=inst.id, subnet_id=s["subnet_id"], cidr=s["cidr"], first_seen_at=now)
                session.add(srow)
                sub_rows[s["subnet_id"]] = srow
            net = nets.get(s["network_id"] or "") or {}
            srow.network_id, srow.name, srow.project_id = s["network_id"], s["name"], s["project_id"]
            srow.network_name = _text(net.get("name")) if net else srow.network_name
            srow.project_name = projects.get(s["project_id"] or "") or srow.project_name
            srow.cidr, srow.ip_version, srow.gateway_ip = s["cidr"], s["ip_version"], s["gateway_ip"]
            srow.allocation_pools, srow.enable_dhcp = s["allocation_pools"], s["enable_dhcp"]
            if net:
                srow.shared = bool(net.get("shared"))
            srow.missing_syncs, srow.last_seen_at = 0, now
            seen_sub.add(s["subnet_id"])
            if not srow.mapped_manually:
                # 唯一才自動：這個連線裡只有它是這個 CIDR（不同 project 的重疊網段不合併），
                # 而且範圍內剛好一個同 CIDR 的 jt-ipam 子網路；其他一律「待對應」
                hits = cands.get(s["cidr"], [])
                srow.mapped_subnet_id = hits[0] if counts[s["cidr"]] == 1 and len(hits) == 1 else None
        for sid, srow in list(sub_rows.items()):
            if sid in seen_sub:
                continue
            srow.missing_syncs += 1
            if srow.missing_syncs >= 2:
                await session.delete(srow)
                del sub_rows[sid]
                summary["removed_subnets"] += 1
    summary["pending_subnets"] = sum(1 for r in sub_rows.values() if r.mapped_subnet_id is None)
    await session.flush()
    mapped = {sid: r.mapped_subnet_id for sid, r in sub_rows.items() if r.mapped_subnet_id is not None}

    # ── VM ──
    by_ext = {row.external_id: row for row in (await session.execute(
        select(VirtualMachine).where(VirtualMachine.cluster_id == cluster.id))).scalars() if row.external_id}
    partial_ids: set[str] = set()
    if col.servers is not None:
        seen_vm: set[str] = set()
        for s in col.servers:
            vrow = by_ext.get(s["id"])
            if vrow is None:
                vrow = VirtualMachine(id=uuid.uuid4(), cluster_id=cluster.id, external_id=s["id"], name=s["name"])
                session.add(vrow)
                by_ext[s["id"]] = vrow
            seen_vm.add(s["id"])
            vrow.status = s["status"]
            if s["partial"]:
                partial_ids.add(s["id"])  # cell 掛掉的部分記錄：其他欄位維持上一次的值
                continue
            vrow.name = s["name"]
            vrow.node = s["node"]
            vrow.kind = "vm"
            vrow.vcpus, vrow.memory_mb, vrow.disk_gb = s["vcpus"], s["memory_mb"], s["disk_gb"]
            vrow.is_template = False
            pname = projects.get(s["project_id"] or "") or s["project_id"]
            vrow.description = f"project: {pname}" if pname else None
        # 範圍縮小時只在自己 project 裡判斷刪除：VM 的 project 記在說明（「project: <名稱>」）
        own = None
        if not col.servers_complete and col.servers_listed and col.scope_project_id:
            own = f"project: {projects.get(col.scope_project_id) or col.scope_project_id}"
        if col.servers_complete or own:
            for ext, vrow in list(by_ext.items()):
                if ext not in seen_vm and (col.servers_complete or vrow.description == own):
                    await session.delete(vrow)
                    del by_ext[ext]
                    summary["removed_vms"] += 1
    await session.flush()

    # ── Port、網卡、IPAM ──
    port_rows = {r.port_id: r for r in (await session.execute(
        select(OpenStackPort).where(OpenStackPort.instance_id == inst.id))).scalars()}
    fip_by_port: dict[str, list[dict[str, Any]]] = {}
    unbound: list[dict[str, Any]] = []
    for f in col.floatingips or []:
        if not isinstance(f, dict):
            continue
        item = {"floating_ip_address": _ip(f.get("floating_ip_address")), "floating_ip_id": _text(f.get("id")),
                "router_id": _text(f.get("router_id"))}
        if f.get("port_id"):
            fip_by_port.setdefault(str(f["port_id"]), []).append(item)
        else:
            unbound.append({**item, "project_id": _text(f.get("project_id") or f.get("tenant_id")),
                            "status": _text(f.get("status"))})
    if col.floatingips is not None:
        col.capabilities["unbound_floating_ips"] = unbound[:2000]

    hn_run = HostnameRun(session, source="openstack", origin=f"openstack:{inst.id}",
                         peers=await enabled_peers(session, OpenStackInstance))
    mac_run = MacRun(session, source="openstack")
    create_cache: dict[Any, Any] = {}
    if col.ports is not None:
        seen_port: set[str] = set()
        nics: dict[Any, list[VMInterface]] = {}
        first_ip: dict[Any, Any] = {}
        for p in col.ports:
            vm = by_ext.get(p["device_id"] or "") if (p["device_owner"] or "").startswith("compute:") else None
            prow = port_rows.get(p["port_id"])
            if prow is None:
                prow = OpenStackPort(instance_id=inst.id, port_id=p["port_id"], first_seen_at=now, floating_ips=[])
                session.add(prow)
                port_rows[p["port_id"]] = prow
            net = nets.get(p["network_id"] or "") or {}
            prow.vm_id = vm.id if vm is not None else None
            prow.device_id, prow.device_owner, prow.network_id = p["device_id"], p["device_owner"], p["network_id"]
            prow.network_name = _text(net.get("name")) if net else prow.network_name
            prow.project_id = p["project_id"]
            prow.project_name = projects.get(p["project_id"] or "") or prow.project_name
            prow.mac, prow.fixed_ips, prow.status = p["mac"], p["fixed_ips"], p["status"]
            prow.binding_host = p["binding_host"]
            if col.floatingips is not None:
                prow.floating_ips = fip_by_port.get(p["port_id"], [])
            prow.missing_syncs, prow.last_seen_at = 0, now
            seen_port.add(p["port_id"])
            if vm is None:
                continue
            # IPv4 優先（IPAM 的子網路、NAT、防火牆規則多半以 IPv4 表達）
            ips = sorted(p["fixed_ips"], key=lambda f: ":" in (f["ip_address"] or ""))
            nics.setdefault(vm.id, []).append(VMInterface(
                vm_id=vm.id, name=p["port_id"][:64], mac=p["mac"],
                primary_ip=ips[0]["ip_address"] if ips else None, bridge=_text(net.get("name")) if net else None))
            name_known = p["device_id"] not in partial_ids
            for f in ips:
                target = mapped.get(f["subnet_id"] or "")
                if target is None:
                    continue                  # 待對應的子網路：不連（重疊網段時連錯比不連糟）
                create_in = None
                if inst.auto_create_ips:
                    if target not in create_cache:
                        create_cache[target] = await addable_subnets(session, {target})
                    create_in = create_cache[target]
                linked = await _link_ip_to_ipam(
                    session, f["ip_address"], p["mac"], vm.name if name_known else None,
                    scope_ids={target}, create_in=create_in, hn_run=hn_run if name_known else None,
                    mac_run=mac_run, source="openstack")
                if linked is not None:
                    summary["ipam_linked"] += 1
                    first_ip.setdefault(vm.id, linked.id)
        if col.ports_complete:
            for pid, prow in list(port_rows.items()):
                if pid in seen_port:
                    continue
                prow.missing_syncs += 1
                if prow.missing_syncs >= 2:
                    await session.delete(prow)
                    summary["removed_ports"] += 1
            # 網卡：清單完整時以 Neutron 為準重建（不完整就保留上一次的）
            await session.flush()
            vm_ids = [r.id for r in by_ext.values()]
            if vm_ids:
                await session.execute(VMInterface.__table__.delete().where(in_values(VMInterface.vm_id, vm_ids)))
            for lst in nics.values():
                session.add_all(lst)
            for r in by_ext.values():
                if r.external_id not in partial_ids:
                    r.primary_ip_id = first_ip.get(r.id)
        else:
            for vid, ipid in first_ip.items():
                vm_row = next((r for r in by_ext.values() if r.id == vid), None)
                if vm_row is not None and vm_row.primary_ip_id is None:
                    vm_row.primary_ip_id = ipid
    await session.flush()
    for r in by_ext.values():
        if r.external_id in partial_ids:
            hn_run.hold(r.name)           # 這一輪看不到名稱：它以前報過的名稱先保留
    await mac_run.finish()
    hn = await hn_run.finish(complete=col.complete)
    if hn.get("breaker"):
        summary["errors"].append(f"hostname cleanup skipped: {hn['breaker']}")

    # ── 連線本身 ──
    prev_caps = inst.capabilities or {}
    caps = dict(col.capabilities)
    if "cubecos" not in caps.get("services", {}) and prev_caps.get("services", {}).get("cubecos") \
            and inst.product == "cubecos":
        caps.setdefault("services", {})["cubecos"] = prev_caps["services"]["cubecos"]
    inst.capabilities = caps
    if col.nodes is not None:
        inst.nodes = col.nodes
    inst.last_sync_at = now
    if col.complete:
        inst.last_full_success_at = now
    inst.last_error = "；".join(summary["errors"])[:2000] if summary["errors"] else None
    summary["complete"] = col.complete
    summary["errors"] = summary["errors"][:20]
    return summary
