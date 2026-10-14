"""Univention UCS DNS adapter — 直接打 UDM REST API（不碰 UCS 內的 BIND9）。

UDM REST API（HAL+JSON）文件：https://docs.software-univention.de/developer-reference/latest/en/udm/rest-api.html
  Base：https://<ucs>/univention/udm
  認證：HTTP Basic（建議用具 DNS 寫權的帳號）
  物件：
    GET {base}/dns/forward_zone/                列正解 zone（properties.zone）
    GET {base}/dns/reverse_zone/                列反解 zone（名稱取 DN 的 zoneName；properties.subnet 是正向寫法）
    GET {base}/dns/host_record/?superordinate=<zoneDN>   A/AAAA（properties.name + properties.a[]）
    GET {base}/dns/alias/?superordinate=<zoneDN>         CNAME（properties.name + properties.cname）
    GET {base}/dns/ptr_record/?superordinate=<zoneDN>    PTR（properties.address + properties.ptr_record）

所有請求走 safe_http（A10 SSRF 守門）；密碼即時解密、不常駐。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from app.core.safe_http import UnsafeOutboundURL, safe_request, transport_detail
from app.services.dns.base import DNSAdapter, DNSAdapterError, DNSRecordOp, DNSZoneInfo

log = logging.getLogger(__name__)


def _mx(v: Any) -> tuple[str, str]:
    """UDM 的 MX：{priority, mailserver}（5.x）或 [priority, mailserver]；回傳（優先序, 不含結尾點的主機）。"""
    if isinstance(v, dict):
        return str(v.get("priority", "")).strip(), str(v.get("mailserver", "")).strip().rstrip(".")
    if isinstance(v, list | tuple) and len(v) == 2:
        return str(v[0]).strip(), str(v[1]).strip().rstrip(".")
    parts = str(v).split()
    return (parts[0], parts[1].rstrip(".")) if len(parts) == 2 else ("", "")


def _is_json(resp: httpx.Response) -> bool:
    return "json" in (resp.headers.get("content-type") or "").lower()


def _json_message(resp: httpx.Response) -> str | None:
    """UDM 的 JSON 錯誤：`{"error": {"message": "..."}, "_links": ...}` 只取 message。"""
    if not _is_json(resp):
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    err = data.get("error") if isinstance(data, dict) else None
    msg = err.get("message") if isinstance(err, dict) else None
    return str(msg) if msg else None


def _brief(text: str, preferred: str | None = None, limit: int = 200) -> str:
    """錯誤回應的摘要：JSON 錯誤只留訊息、HTML 錯誤頁只留標題（或去掉標籤的文字），不整包塞進錯誤訊息。"""
    import re
    if preferred:
        return " ".join(preferred.split())[:limit]
    t = text or ""
    if "<" in t and re.search(r"<\s*(html|!doctype|body|title)\b", t, re.I):
        m = re.search(r"<title[^>]*>(.*?)</title>", t, re.I | re.S)
        t = m.group(1) if m else re.sub(r"<[^>]+>", " ", t)
    return " ".join(t.split())[:limit]


class UniventionUCSAdapter(DNSAdapter):
    type = "univention_ucs"

    def __init__(self, *, api_url: str, username: str, password: str, verify_tls: bool = True) -> None:
        # api_url 可給 https://ucs 或 https://ucs/univention/udm，統一正規化到 .../univention/udm
        base = api_url.rstrip("/")
        if not base.endswith("/univention/udm"):
            base = base + "/univention/udm"
        self.base = base
        self.username = username
        self.password = password
        self.verify_tls = verify_tls

    @property
    def _headers(self) -> dict[str, str]:
        # UCS UDM 對空帳號/密碼會回含糊的「basic auth credentials are malformed」400；
        # 先在這裡擋下並給可行動訊息（實際案例：帳號被清空、只剩密碼）。
        if not self.username or not self.password:
            raise DNSAdapterError(
                "UCS 認證不完整（帳號或密碼為空）；basic auth 需要帳號與密碼，"
                "請到「DNS 伺服器」設定重新輸入 UCS 帳號密碼。",
                code="dns_ucs_incomplete_auth",
            )
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        return {"Accept": "application/json", "Authorization": f"Basic {token}"}

    async def _get(self, path: str, params: dict | None = None) -> dict:  # type: ignore[type-arg]
        url = f"{self.base}{path}"
        try:
            resp = await safe_request(
                "GET", url, headers=self._headers, params=params or {},
                timeout=20.0, verify=self.verify_tls,
            )
        except UnsafeOutboundURL as exc:
            raise DNSAdapterError(f"SSRF guard rejected URL: {exc}",
                                  code="dns_ssrf", reason=str(exc)[:200]) from exc
        except httpx.HTTPError as exc:
            raise DNSAdapterError(f"transport: {transport_detail(exc)}",
                                  code="dns_transport",
                                  reason=transport_detail(exc)) from exc
        if resp.status_code == 401:
            raise DNSAdapterError("UCS UDM REST 401 —— 帳號／密碼或權限不足",
                                  code="dns_ucs_401")
        if resp.status_code == 404 and not _is_json(resp):
            # Apache 自己的 404＝這台沒有 /univention/udm/ 的轉送，也就是沒裝 UDM REST API
            # （Primary／Backup Directory Node 預設才有；使用者 2026-10-14 加的 Backup 就是沒有）。
            # UDM 本身的 404（物件不存在）是 JSON，不會走到這裡
            root = f"{self.base}/"
            raise DNSAdapterError(
                f"這台 UCS 沒有 UDM REST API（{root} 回 404）：在那台執行 "
                "univention-install univention-directory-manager-rest，"
                "或改填 Primary Directory Node 的位址",
                code="dns_ucs_no_rest", url=root)
        body = _brief(resp.text, _json_message(resp))
        if resp.status_code == 403 and "allowed groups" in body.lower():
            # UDM REST API 只讓 directory/manager/rest/authorized-groups/* 列出的群組成員使用。
            # 手動裝在 Replica 上的常少了這組設定：同一個帳號別台可以、這台不行（使用者 2026-10-14）
            raise DNSAdapterError(
                f"帳號 {self.username} 不在這台 UCS 允許使用 UDM REST API 的群組裡：在那台用 "
                "ucr set directory/manager/rest/authorized-groups/<名稱>=<群組 DN> 加入帳號所在的群組"
                "（例如 Domain Admins），再重新啟動 univention-directory-manager-rest",
                code="dns_ucs_not_allowed", user=self.username)
        if resp.status_code != 200:
            raise DNSAdapterError(f"UCS UDM REST {resp.status_code}: {body}",
                                  code="dns_ucs_http", status=resp.status_code, body=body)
        return resp.json()  # type: ignore[no-any-return]

    @staticmethod
    def _objects(data: dict) -> list[dict]:  # type: ignore[type-arg]
        emb = (data.get("_embedded") or {})
        objs = emb.get("udm:object") or emb.get("udm:objects") or []
        return objs if isinstance(objs, list) else [objs]

    async def healthcheck(self) -> dict[str, object]:
        data = await self._get("/dns/forward_zone/", {"limit": 1})
        return {"ok": True, "total": data.get("total")}

    async def list_zones(self) -> list[DNSZoneInfo]:
        out: list[DNSZoneInfo] = []
        fwd = await self._get("/dns/forward_zone/", {"limit": 10000})
        for o in self._objects(fwd):
            zone = (o.get("properties") or {}).get("zone")
            if zone:
                out.append(DNSZoneInfo(name=str(zone).rstrip("."), kind="forward"))
        try:
            rev = await self._get("/dns/reverse_zone/", {"limit": 10000})
            for o in self._objects(rev):
                rev_name = self._reverse_zone_name(o)
                if rev_name:
                    out.append(DNSZoneInfo(name=rev_name, kind="reverse"))
        except DNSAdapterError:
            pass
        return out

    @staticmethod
    def _reverse_zone_name(o: dict) -> str | None:  # type: ignore[type-arg]
        """反解 zone 的名稱：DN 的第一段就是 `zoneName=2.0.192.in-addr.arpa`（IPv6 也是），
        沒有 DN 時才用 properties.subnet（UDM 寫正向的 "192.0.2"，倒過來接 in-addr.arpa；只適用 IPv4）。"""
        first = str(o.get("dn") or "").split(",", 1)[0]
        if first.lower().startswith("zonename="):
            name = first.split("=", 1)[1].strip().rstrip(".")
            if name:
                return name
        subnet = str((o.get("properties") or {}).get("subnet") or "")
        if subnet and ":" not in subnet:
            return ".".join(reversed(subnet.split("."))) + ".in-addr.arpa"
        return None

    async def _zone_obj(self, zone_name: str) -> dict | None:  # type: ignore[type-arg]
        want = zone_name.rstrip(".").lower()
        if want.endswith((".in-addr.arpa", ".ip6.arpa")):
            data = await self._get("/dns/reverse_zone/", {"limit": 10000})
            for o in self._objects(data):
                if (self._reverse_zone_name(o) or "").lower() == want:
                    return o
            return None
        data = await self._get("/dns/forward_zone/", {"limit": 10000})
        for o in self._objects(data):
            if str((o.get("properties") or {}).get("zone", "")).rstrip(".").lower() == want:
                return o
        return None

    async def _zone_dn(self, zone_name: str) -> str | None:
        o = await self._zone_obj(zone_name)
        return o.get("dn") if o else None

    async def _optional(self, path: str, dn: str) -> list[dict]:  # type: ignore[type-arg]
        """別名、SRV、TXT、NS 委派：只有比對群組勾了才用得到，讀不到不讓整個 zone 失敗，但要留紀錄
        （以前別名讀失敗直接吞掉，看不出少了什麼）。"""
        try:
            return self._objects(await self._get(path, {"superordinate": dn, "limit": 10000}))
        except DNSAdapterError as exc:
            log.warning("ucs %s %s: %s", self.base, path, exc)
            return []

    async def list_records(self, zone_name: str) -> list[DNSRecordOp]:
        zone = zone_name.rstrip(".")
        zobj = await self._zone_obj(zone)
        dn = zobj.get("dn") if zobj else None
        if not dn:
            return []
        out: list[DNSRecordOp] = []

        def _fqdn(label: str) -> str:
            label = (label or "").strip()
            if not label or label in ("@", zone):
                return zone
            return f"{label}.{zone}"

        if zone.lower().endswith((".in-addr.arpa", ".ip6.arpa")):
            # PTR：address 是 zone 內的相對名稱（例 "145"），ptr_record 是 FQDN 清單（帶結尾點）
            ptrs = await self._get("/dns/ptr_record/", {"superordinate": dn, "limit": 10000})
            for o in self._objects(ptrs):
                p = o.get("properties") or {}
                name = _fqdn(str(p.get("address", "")))
                for target in (p.get("ptr_record") or []):
                    if str(target).strip():
                        out.append(DNSRecordOp(name=name, type="PTR", value=str(target).strip().rstrip(".")))
            return out

        def _host_props(name: str, p: dict) -> None:  # type: ignore[type-arg]
            # A / AAAA（a 同時含 v4/v6，用冒號判斷）；MX、TXT 也掛在同一個物件上
            for ip in (p.get("a") or []):
                out.append(DNSRecordOp(name=name, type="AAAA" if ":" in str(ip) else "A", value=str(ip)))
            for mx in (p.get("mx") or []):
                pr, host = _mx(mx)
                if host:
                    out.append(DNSRecordOp(name=name, type="MX", value=f"{pr} {host}"))
            for txt in (p.get("txt") or []):
                if str(txt).strip():
                    out.append(DNSRecordOp(name=name, type="TXT", value=str(txt)))

        # 區域頂端（zone 本身）：A／AAAA、NS、MX、TXT 在 forward_zone 物件上（以前連頂端的 A 都沒拉）
        zp = (zobj or {}).get("properties") or {}
        _host_props(zone, zp)
        for ns in (zp.get("nameserver") or []):
            if str(ns).strip():
                out.append(DNSRecordOp(name=zone, type="NS", value=str(ns).strip().rstrip(".")))

        hosts = await self._get("/dns/host_record/", {"superordinate": dn, "limit": 10000})
        for o in self._objects(hosts):
            p = o.get("properties") or {}
            _host_props(_fqdn(str(p.get("name", ""))), p)
        # CNAME（alias）
        for o in await self._optional("/dns/alias/", dn):
            p = o.get("properties") or {}
            if str(p.get("cname", "")).strip():
                out.append(DNSRecordOp(name=_fqdn(str(p.get("name", ""))),
                                       type="CNAME", value=str(p.get("cname", "")).strip().rstrip(".")))
        # SRV：name 是 {service, protocol, extension}；權重欄位 UDM 拼成 weigtht（也接受 weight）
        for o in await self._optional("/dns/srv_record/", dn):
            p = o.get("properties") or {}
            nm = p.get("name") or {}
            if isinstance(nm, dict):
                label = ".".join(x for x in (f"_{nm.get('service')}" if nm.get("service") else "",
                                             f"_{nm.get('protocol')}" if nm.get("protocol") else "",
                                             str(nm.get("extension") or "")) if x)
            else:
                label = str(nm)
            for loc in (p.get("location") or []):
                if isinstance(loc, dict):
                    vals = [loc.get("priority"), loc.get("weigtht", loc.get("weight")), loc.get("port"), loc.get("server")]
                else:
                    vals = list(loc)[:4]
                if len(vals) == 4 and all(v not in (None, "") for v in vals):
                    out.append(DNSRecordOp(name=_fqdn(label), type="SRV",
                                           value=f"{vals[0]} {vals[1]} {vals[2]} {str(vals[3]).rstrip('.')}"))
        for o in await self._optional("/dns/txt_record/", dn):
            p = o.get("properties") or {}
            for txt in (p.get("txt") or []):
                if str(txt).strip():
                    out.append(DNSRecordOp(name=_fqdn(str(p.get("name", ""))), type="TXT", value=str(txt)))
        # NS 委派（子網域交給別台）：zone 是相對名稱
        for o in await self._optional("/dns/ns_record/", dn):
            p = o.get("properties") or {}
            for ns in (p.get("nameserver") or []):
                if str(ns).strip():
                    out.append(DNSRecordOp(name=_fqdn(str(p.get("zone", ""))), type="NS",
                                           value=str(ns).strip().rstrip(".")))
        return out

    # ── 寫入路徑（建立/刪除 host_record）──
    # 註：UCS 寫入需帳號具 DNS 寫權；不同 UCS 版本 properties 形狀可能略異，
    # 上線前請對實際 UCS 驗證一次。
    async def _modify(self, method: str, path: str, body: dict | None = None) -> dict:  # type: ignore[type-arg]
        url = f"{self.base}{path}"
        try:
            resp = await safe_request(
                method, url, headers={**self._headers, "Content-Type": "application/json"},
                json=body, timeout=30.0, verify=self.verify_tls,
            )
        except UnsafeOutboundURL as exc:
            raise DNSAdapterError(f"SSRF guard rejected URL: {exc}",
                                  code="dns_ssrf", reason=str(exc)[:200]) from exc
        if resp.status_code not in (200, 201, 204):
            raise DNSAdapterError(f"UCS UDM {method} {resp.status_code}: {_brief(resp.text, _json_message(resp))}",
                                  code="dns_ucs_http_method", method=method,
                                  status=resp.status_code, body=_brief(resp.text, _json_message(resp)))
        return resp.json() if resp.content else {}

    async def upsert_record(self, zone_name: str, op: DNSRecordOp) -> None:
        if op.type not in ("A", "AAAA"):
            raise DNSAdapterError(f"UCS adapter 目前只支援推送 A/AAAA（收到 {op.type}）",
                                  code="dns_ucs_type_unsupported", value=op.type)
        zone = zone_name.rstrip(".")
        dn = await self._zone_dn(zone)
        if not dn:
            raise DNSAdapterError(f"UCS 找不到 forward zone {zone}",
                                  code="dns_ucs_zone_not_found", zone=zone)
        label = op.name.rstrip(".")
        label = label[: -len(zone) - 1] if label.endswith("." + zone) else label
        if label == zone:
            label = "@"
        body = {"position": dn, "superordinate": dn, "properties": {"name": label, "a": [op.value]}}
        await self._modify("POST", "/dns/host_record/", body)

    async def delete_record(self, zone_name: str, op: DNSRecordOp) -> None:
        zone = zone_name.rstrip(".")
        dn = await self._zone_dn(zone)
        if not dn:
            return
        hosts = await self._get("/dns/host_record/", {"superordinate": dn, "limit": 10000})
        target_label = op.name.rstrip(".")
        target_label = target_label[: -len(zone) - 1] if target_label.endswith("." + zone) else target_label
        for o in self._objects(hosts):
            p = o.get("properties") or {}
            if str(p.get("name", "")) == target_label and op.value in (p.get("a") or []):
                self_link = (((o.get("_links") or {}).get("self") or {}) or {}).get("href")
                if self_link:
                    # self_link 是完整 URL；safe_request 會再過 SSRF 檢查
                    try:
                        await safe_request("DELETE", self_link, headers=self._headers,
                                           timeout=30.0, verify=self.verify_tls)
                    except UnsafeOutboundURL as exc:
                        raise DNSAdapterError(f"SSRF guard rejected URL: {exc}",
                                  code="dns_ssrf", reason=str(exc)[:200]) from exc
                return
