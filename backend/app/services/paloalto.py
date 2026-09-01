"""Palo Alto（PAN-OS）唯讀同步。**只打 GET，永遠不寫入防火牆。**

# 為什麼這支比 FortiGate 複雜

PAN-OS 把「設定」與「執行時期狀態」放在兩套不同的 API，而且回應格式不同：

| 要的資料 | API | 端點 | 回應 |
|---|---|---|---|
| 安全政策 / NAT / 位址物件 | REST | `/restapi/<版本>/Policies/SecurityRules` … | JSON |
| ARP 表 / DHCP 租約 | XML（op） | `/api/?type=op&cmd=<show>…</show>` | **只有 XML** |
| vsys 清單 | XML（config） | `/api/?type=config&action=get&xpath=…` | **只有 XML** |

所以這支同時要能吃 JSON 與 XML —— XML 一律走 `defusedxml`，不用標準函式庫的解析器
（外部裝置回來的內容算不受信任的輸入）。

# 三個最容易在別人機器上壞掉的地方

1. **REST URI 裡的版本段綁 PAN-OS 版本**（`v10.1` / `v10.2` / `v11.0` / `v11.1`…）。
   寫死一個值，換一台版本不同的機器就整批 404。留空時由 `show system info` 的
   `sw-version` 推導，並把結果記在實例上。
2. **多 vsys**：`location=vsys&vsys=<名稱>`；另有 `location=shared` 的共用物件 ——
   共用物件在每個 vsys 都看得到，不特別標示會在畫面上重複出現、看起來像同步壞掉。
3. **PAN-OS 的規則沒有數字 id**，名稱就是識別（vsys 內唯一），順序本身也有語意
   （由上而下比對）—— 所以要存 `position`。

# 沒有實機

與 FortiGate 那次一樣：**依官方文件實作 + 容錯解析 + 逐端點診斷**。
拿不到的欄位就留空，不要用推測補值 —— 猜出來的資料看起來跟真的一樣，那更危險。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from defusedxml import ElementTree as DefusedET
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import UnsafeOutboundURL, safe_request, transport_detail
from app.core.security import decrypt_secret, encrypt_secret
from app.models.address import IPAddress
from app.models.paloalto import PaloAltoAddressObject, PaloAltoFirewall, PaloAltoPolicy
from app.services.hostname import apply_observation


class PaloAltoError(RuntimeError):
    """與這台 PAN-OS 溝通時的錯誤（訊息會直接顯示給管理員，要講得出下一步）。"""


#: 診斷時每支端點的逾時。並行探測，所以最壞情況約等於單一逾時。
_DIAG_TIMEOUT = 8.0

#: REST 資源路徑（版本段另外組）。名稱與 PAN-OS 官方文件一致。
R_SECURITY = "Policies/SecurityRules"
R_NAT = "Policies/NATRules"
R_ADDRESS = "Objects/Addresses"
R_ADDRGRP = "Objects/AddressGroups"

#: 已知的 REST 版本段，由新到舊。推導不出來時依序試。
KNOWN_API_VERSIONS = ("v11.1", "v11.0", "v10.2", "v10.1", "v10.0", "v9.1")


def _aad(fw_id: uuid.UUID) -> bytes:
    return f"paloalto_firewall:{fw_id}:api_key".encode()


def encrypt_api_key(fw_id: uuid.UUID, key: str) -> tuple[bytes, bytes]:
    """回 (密文, nonce) 兩個 bytea 欄位的值。

    ⚠️ 不要改用 `envelope_encrypt` —— 那個回的是四個欄位的 dict（給 JSONB 存的），
    塞進這裡的兩個 bytea 欄位會在建立實例時就炸掉。比照 FortiGate 用 encrypt_secret。
    """
    return encrypt_secret(key, aad=_aad(fw_id))


def _decrypt_key(fw: PaloAltoFirewall) -> str:
    return decrypt_secret(fw.api_key_enc, fw.api_key_nonce, aad=_aad(fw.id)).decode("utf-8")


# ─────────────────── 傳輸層 ───────────────────
async def _request(
    fw: PaloAltoFirewall, *, path: str, params: dict[str, Any], timeout: float = 15.0,
) -> httpx.Response:
    url = f"{fw.api_url.rstrip('/')}{path}"
    headers = {"X-PAN-KEY": _decrypt_key(fw), "Accept": "application/json"}
    try:
        resp = await safe_request(
            "GET", url, headers=headers, params=params, timeout=timeout, verify=fw.verify_tls,
        )
    except UnsafeOutboundURL as exc:
        raise PaloAltoError(f"SSRF guard rejected URL: {exc}") from exc
    except httpx.HTTPError as exc:
        # 連線類錯誤一定要帶底層原文：ConnectError 分不出 DNS／拒絕／路由／憑證
        raise PaloAltoError(f"transport: {transport_detail(exc)}") from exc
    if resp.status_code in (401, 403):
        raise PaloAltoError(
            f"{resp.status_code} 未授權：請確認 API 金鑰正確、該管理員角色可讀取此資源，"
            "且來源 IP 在「Permitted IP Addresses」允許範圍內",
        )
    return resp


async def _rest_get(
    fw: PaloAltoFirewall, resource: str, *, vsys: str | None = None,
    shared: bool = False, version: str | None = None, timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """REST（JSON）取一個資源，回傳 `result.entry` 清單。

    外層是 `{"@status","@code","result":{"@count","@total-count","entry":[…]}}`；
    `entry` 只有一筆時某些版本會回物件而非陣列 —— 兩種都要吃。
    """
    ver = version or fw.api_version or KNOWN_API_VERSIONS[0]
    params: dict[str, Any] = {"location": "shared"} if shared else {
        "location": "vsys", "vsys": vsys or "vsys1",
    }
    resp = await _request(
        fw, path=f"/restapi/{ver}/{resource}", params=params, timeout=timeout,
    )
    if resp.status_code == 404:
        raise PaloAltoError(
            f"{resource} 回 404：REST 版本段 {ver} 與這台 PAN-OS 不符，"
            "請在設定頁指定正確的 API 版本（或留空讓系統自行偵測）",
        )
    if resp.status_code != 200:
        raise PaloAltoError(f"GET {resource}: {resp.status_code} {resp.text[:200]}")
    try:
        body = json.loads(resp.text)
    except ValueError as exc:
        ctype = resp.headers.get("content-type", "?")
        snippet = " ".join(resp.text[:120].split())
        raise PaloAltoError(
            f"回應不是 JSON（{resource}）：{exc} content-type={ctype} 內容開頭={snippet!r}",
        ) from exc
    if str(body.get("@status", "success")).lower() != "success":
        raise PaloAltoError(f"{resource}: {body.get('message') or body}")
    result = body.get("result") or {}
    entry = result.get("entry")
    if entry is None:
        return []
    return entry if isinstance(entry, list) else [entry]


async def _xml_get(
    fw: PaloAltoFirewall, params: dict[str, Any], *, timeout: float = 15.0,
) -> Any:
    """XML API（op / config）。回傳 `<result>` 元素；沒有 result 就回 None。

    PAN-OS 的 op 指令**只回 XML**，沒有 JSON 可選 —— 這是與 FortiGate 最大的差異，
    也是這支服務必須同時處理兩種格式的原因。
    """
    resp = await _request(fw, path="/api/", params=params, timeout=timeout)
    if resp.status_code != 200:
        raise PaloAltoError(f"XML API: {resp.status_code} {resp.text[:200]}")
    try:
        root = DefusedET.fromstring(resp.text)
    except Exception as exc:      # defusedxml 會丟多種例外，一律當成「不是 XML」
        snippet = " ".join(resp.text[:120].split())
        raise PaloAltoError(f"回應不是 XML：{exc} 內容開頭={snippet!r}") from exc
    if root.get("status") != "success":
        msg = "".join(root.itertext()).strip()[:200]
        raise PaloAltoError(f"PAN-OS 回報失敗：{msg or root.get('status')}")
    return root.find("result")


# ─────────────────── 小工具 ───────────────────
def _members(v: Any) -> str | None:
    """PAN-OS 的清單長這樣：`{"member": ["a","b"]}` → `"a, b"`。"""
    if v is None:
        return None
    if isinstance(v, dict):
        m = v.get("member")
        if isinstance(m, list):
            return ", ".join(str(x) for x in m) or None
        if m is not None:
            return str(m)
        return None
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) or None
    return str(v) or None


def _valid_ip(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip().split("/")[0]
    if not s:
        return None
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def _norm_mac(v: object) -> str | None:
    if v is None:
        return None
    s = re.sub(r"[^0-9a-fA-F]", "", str(v))
    if len(s) != 12:
        return None
    return ":".join(s[i:i + 2] for i in range(0, 12, 2)).lower()


def _addr_value(e: dict[str, Any]) -> tuple[str | None, str | None]:
    """PAN-OS 用**欄位名**表示位址型別，不是另外一個 type 欄位。"""
    for key in ("ip-netmask", "ip-range", "ip-wildcard", "fqdn"):
        if e.get(key):
            return key, str(e[key])
    return None, None


def _scope(fw: PaloAltoFirewall) -> list[uuid.UUID] | None:
    return list(fw.scope_subnet_ids) if fw.scope_subnet_ids else None


# ─────────────────── 版本與 vsys 探索 ───────────────────
async def detect_api_version(fw: PaloAltoFirewall) -> str:
    """由 `show system info` 的 `sw-version` 推導 REST 版本段。

    PAN-OS 回的是 `11.1.4-h7` 這種字串，REST URI 要的是 `v11.1` —— 取前兩段。
    推導不出來就退回已知清單的第一個，讓後續的 404 訊息去指引使用者手動指定。
    """
    try:
        result = await _xml_get(
            fw, {"type": "op", "cmd": "<show><system><info/></system></show>"}, timeout=10.0,
        )
    except PaloAltoError:
        return KNOWN_API_VERSIONS[0]
    node = result.find(".//sw-version") if result is not None else None
    raw = (node.text or "").strip() if node is not None else ""
    m = re.match(r"(\d+)\.(\d+)", raw)
    if not m:
        return KNOWN_API_VERSIONS[0]
    ver = f"v{m.group(1)}.{m.group(2)}"
    return ver if ver in KNOWN_API_VERSIONS else KNOWN_API_VERSIONS[0]


async def list_vsys(fw: PaloAltoFirewall) -> list[str]:
    """要同步的 vsys：使用者指定優先；否則從設定檔探索；失敗退回 `['vsys1']`。"""
    if fw.vsys_list:
        return [v for v in fw.vsys_list if v]
    try:
        result = await _xml_get(fw, {
            "type": "config", "action": "get",
            "xpath": "/config/devices/entry/vsys",
        }, timeout=10.0)
    except PaloAltoError:
        return ["vsys1"]        # 單一 vsys 機型或權限不足
    if result is None:
        return ["vsys1"]
    names = [e.get("name") for e in result.iter("entry") if e.get("name")]
    return names or ["vsys1"]


# ─────────────────── IP stamp（重疊網段安全）───────────────────
async def _stamp_ip_seen(
    session: AsyncSession, ip: str, *, evidence: str,
    mac: str | None = None, hostname: str | None = None,
    subnet_ids: list[uuid.UUID] | None = None, dhcp: bool = False,
    permanent: bool = False,
) -> bool:
    """只標記**既有**的 IP，絕不新建（與其他防火牆整合一致）。

    `evidence`＝證據契約裡的來源名稱（`arp:paloalto` / `lease:paloalto`），
    逐來源存在 `arp_seen`（見 services/arp_seen.py）。
    """
    ipx = _valid_ip(ip)
    if ipx is None:
        return False
    stmt = select(IPAddress).where(IPAddress.ip == ipx)
    if subnet_ids:
        stmt = stmt.where(IPAddress.subnet_id.in_(subnet_ids))
    # 重疊網段下同一個 IP 會有多筆 → 取一筆，不可以用 scalar_one_or_none（會炸掉整批）
    ipa = (await session.execute(stmt.limit(1))).scalars().first()
    if ipa is None:
        return False
    from app.services import arp_seen as arp_seen_svc
    arp_seen_svc.stamp(ipa, evidence, permanent=permanent)
    if dhcp:
        ipa.in_dhcp_lease = True
    if mac:
        from app.services.arp_precedence import consider_mac
        await consider_mac(session, ip=ipa, mac=mac, source="paloalto")
    if hostname:
        await apply_observation(session, ip=ipa, source="paloalto", hostname=hostname)
    return True


# ─────────────────── 各項同步 ───────────────────
async def sync_arp(session: AsyncSession, fw: PaloAltoFirewall) -> int:
    """ARP 表（XML op）。`<show><arp><entry name='all'/></arp></show>`"""
    result = await _xml_get(fw, {
        "type": "op", "cmd": "<show><arp><entry name='all'/></arp></show>",
    })
    if result is None:
        return 0
    scope_ids = _scope(fw)
    seen = 0
    for e in result.iter("entry"):
        ip = (e.findtext("ip") or "").strip()
        mac = (e.findtext("mac") or "").strip()
        # PAN-OS 的 status：`c`＝complete、`s`＝static。靜態項目不會逾時淘汰，
        # 拿它宣稱上線就等於在說「這台永遠活著」。
        perm = (e.findtext("status") or "").strip().lower() in ("s", "static")
        if await _stamp_ip_seen(session, ip, evidence="arp:paloalto", mac=_norm_mac(mac),
                                subnet_ids=scope_ids, permanent=perm):
            seen += 1
    return seen


async def sync_dhcp_leases(session: AsyncSession, fw: PaloAltoFirewall) -> int:
    """DHCP 租約（XML op）。只標既有 IP，不新建。"""
    result = await _xml_get(fw, {
        "type": "op",
        "cmd": "<show><dhcp><server><lease><interface>all</interface></lease>"
               "</server></dhcp></show>",
    })
    if result is None:
        return 0
    scope_ids = _scope(fw)
    seen = 0
    for e in result.iter("entry"):
        ip = (e.findtext("ip") or "").strip()
        mac = (e.findtext("mac") or "").strip()
        host = (e.findtext("hostname") or "").strip() or None
        if await _stamp_ip_seen(
            session, ip, evidence="lease:paloalto", mac=_norm_mac(mac), hostname=host,
            subnet_ids=scope_ids, dhcp=True,
        ):
            seen += 1
    return seen


async def sync_policies(session: AsyncSession, fw: PaloAltoFirewall, vsys_list: list[str]) -> int:
    """安全政策鏡像。整批取代該 vsys 的內容（來源才是真相）。"""
    now = datetime.now(UTC)
    total = 0
    for vsys in vsys_list:
        rows = await _rest_get(fw, R_SECURITY, vsys=vsys)
        existing = {
            p.name: p for p in (await session.execute(
                select(PaloAltoPolicy).where(
                    PaloAltoPolicy.firewall_id == fw.id, PaloAltoPolicy.vsys == vsys,
                )
            )).scalars().all()
        }
        seen: set[str] = set()
        for idx, e in enumerate(rows):
            name = str(e.get("@name") or "").strip()
            if not name:
                continue
            seen.add(name)
            p = existing.get(name) or PaloAltoPolicy(
                firewall_id=fw.id, vsys=vsys, name=name)
            # 順序本身有語意（PAN-OS 由上而下比對），所以要存
            p.position = idx + 1
            p.action = str(e.get("action") or "") or None
            # PAN-OS 用 "yes"/"no" 而不是布林
            dis = e.get("disabled")
            p.disabled = str(dis).lower() == "yes" if dis is not None else None
            p.from_zone = _members(e.get("from"))
            p.to_zone = _members(e.get("to"))
            p.source = _members(e.get("source"))
            p.destination = _members(e.get("destination"))
            p.application = _members(e.get("application"))
            p.service = _members(e.get("service"))
            p.description = str(e.get("description") or "") or None
            p.raw = e
            p.last_sync_at = now
            if p.id is None:
                session.add(p)
            total += 1
        for name, obj in existing.items():
            if name not in seen:
                await session.delete(obj)
    return total


async def sync_addresses(session: AsyncSession, fw: PaloAltoFirewall, vsys_list: list[str]) -> int:
    """位址物件與群組。**共用（shared）物件單獨標記**，否則會在每個 vsys 重複出現。"""
    now = datetime.now(UTC)
    total = 0
    # shared 只需要抓一次；vsys 各自再抓自己的
    targets: list[tuple[str, bool]] = [("shared", True)] + [(v, False) for v in vsys_list]
    for label, is_shared in targets:
        for resource, kind in ((R_ADDRESS, "address"), (R_ADDRGRP, "group")):
            try:
                rows = await _rest_get(
                    fw, resource, vsys=None if is_shared else label, shared=is_shared)
            except PaloAltoError:
                # 某些機型沒有 shared 物件或該資源讀不到 —— 不該讓整批位址同步失敗
                continue
            existing = {
                a.name: a for a in (await session.execute(
                    select(PaloAltoAddressObject).where(
                        PaloAltoAddressObject.firewall_id == fw.id,
                        PaloAltoAddressObject.vsys == label,
                        PaloAltoAddressObject.kind == kind,
                    )
                )).scalars().all()
            }
            seen: set[str] = set()
            for e in rows:
                name = str(e.get("@name") or "").strip()
                if not name:
                    continue
                seen.add(name)
                a = existing.get(name) or PaloAltoAddressObject(
                    firewall_id=fw.id, vsys=label, name=name, kind=kind)
                if kind == "address":
                    a.obj_type, a.value = _addr_value(e)
                    a.members = None
                else:
                    a.obj_type = "static" if e.get("static") else "dynamic"
                    a.value = _members(e.get("static")) or _members(e.get("dynamic"))
                    m = e.get("static") or {}
                    a.members = m.get("member") if isinstance(m, dict) else None
                a.description = str(e.get("description") or "") or None
                a.last_sync_at = now
                if a.id is None:
                    session.add(a)
                total += 1
            for name, obj in existing.items():
                if name not in seen:
                    await session.delete(obj)
    return total


async def sync_nat(session: AsyncSession, fw: PaloAltoFirewall, vsys_list: list[str]) -> int:
    """NAT 政策 → 共用的 `nat_translations`（`source_origin = paloalto:<id>`）。

    只收**目的地轉換（destination-translation）**：那才是「對外開了什麼」。
    來源轉換（source-translation）是出向流量，混進對外開放清單會製造假曝險。
    """
    from app.models.nat import NATTranslation

    now = datetime.now(UTC)
    origin = f"paloalto:{fw.id}"
    total = 0
    existing = {
        n.external_id: n for n in (await session.execute(
            select(NATTranslation).where(NATTranslation.source_origin == origin)
        )).scalars().all()
    }
    seen: set[str] = set()
    for vsys in vsys_list:
        for e in await _rest_get(fw, R_NAT, vsys=vsys):
            name = str(e.get("@name") or "").strip()
            if not name:
                continue
            dnat = e.get("destination-translation") or {}
            if not isinstance(dnat, dict) or not dnat:
                continue          # 沒有目的地轉換 → 不是「對外開放」
            ext_id = f"{vsys}:{name}"[:200]
            seen.add(ext_id)
            n = existing.get(ext_id) or NATTranslation(
                source_origin=origin, external_id=ext_id)
            n.name = name[:255]
            n.type = "port_forward"
            n.description = str(e.get("description") or "") or None
            # protocol 是 String(8) 且不可為空 —— PAN-OS 的 service 是物件名稱
            # （如 service-http），塞不進去也不該截斷成半個名字，統一記 any，
            # 細節留在 raw 對應的規則裡。
            n.protocol = "any"
            n.updated_at = now
            if n.id is None:
                session.add(n)
            total += 1
    for ext_id, obj in existing.items():
        if ext_id not in seen:
            await session.delete(obj)
    return total


# ─────────────────── 診斷 ───────────────────
async def diagnose(fw: PaloAltoFirewall) -> dict[str, Any]:
    """測試連線：逐端點回報通不通與筆數。沒有實機時，這是收斂欄位的主要依據。"""
    out: dict[str, Any] = {"api_url": fw.api_url}
    version = fw.api_version or await detect_api_version(fw)
    out["api_version"] = version
    out["api_version_detected"] = not fw.api_version
    vsys_list = await list_vsys(fw)
    out["vsys"] = vsys_list
    first = vsys_list[0] if vsys_list else "vsys1"

    async def _rest_probe(label: str, resource: str) -> dict[str, Any]:
        try:
            rows = await _rest_get(
                fw, resource, vsys=first, version=version, timeout=_DIAG_TIMEOUT)
            return {"endpoint": label, "api": "rest", "ok": True, "rows": len(rows)}
        except PaloAltoError as exc:
            return {"endpoint": label, "api": "rest", "ok": False, "error": str(exc)[:200]}

    async def _xml_probe(label: str, cmd: str) -> dict[str, Any]:
        try:
            result = await _xml_get(
                fw, {"type": "op", "cmd": cmd}, timeout=_DIAG_TIMEOUT)
            n = len(list(result.iter("entry"))) if result is not None else 0
            return {"endpoint": label, "api": "xml", "ok": True, "rows": n}
        except PaloAltoError as exc:
            return {"endpoint": label, "api": "xml", "ok": False, "error": str(exc)[:200]}

    # 並行：循序跑對不可達的主機會累加成 N × 逾時，畫面看起來像凍住
    checks = list(await asyncio.gather(
        _rest_probe("security_rules", R_SECURITY),
        _rest_probe("nat_rules", R_NAT),
        _rest_probe("addresses", R_ADDRESS),
        _rest_probe("address_groups", R_ADDRGRP),
        _xml_probe("arp", "<show><arp><entry name='all'/></arp></show>"),
        _xml_probe(
            "dhcp_leases",
            "<show><dhcp><server><lease><interface>all</interface></lease>"
            "</server></dhcp></show>",
        ),
    ))
    out["checks"] = checks
    out["ok_count"] = sum(1 for c in checks if c["ok"])
    return out


# ─────────────────── 主流程 ───────────────────
async def sync_instance(session: AsyncSession, fw: PaloAltoFirewall) -> dict[str, Any]:
    """跑此實例所有啟用的同步；設定 `last_sync_at` / `last_error`。

    **每個區段各自隔離**：實機上很常見某支端點在該版本不存在、或 API 角色讀不到。
    不隔離的話一支掛掉會讓其他全部不同步，畫面上卻只看得到一行錯誤。
    """
    if not fw.api_version:
        # 偵測到就記下來，之後不必每輪再問一次
        fw.api_version = await detect_api_version(fw)
    vsys_list = await list_vsys(fw)
    counts: dict[str, Any] = {"vsys": len(vsys_list), "api_version": fw.api_version}
    errors: dict[str, str] = {}

    async def _section(name: str, coro_factory: Any) -> None:
        try:
            counts[name] = await coro_factory()
        except PaloAltoError as exc:
            errors[name] = str(exc)[:200]

    if fw.sync_arp:
        await _section("arp", lambda: sync_arp(session, fw))
    if fw.sync_dhcp:
        await _section("dhcp", lambda: sync_dhcp_leases(session, fw))
    if fw.sync_policies:
        await _section("policies", lambda: sync_policies(session, fw, vsys_list))
        if "policies" not in errors:
            # 規則異動偵測：與 OPNsense / pfSense / FortiGate 同一套
            from app.services.fw_review import run_sentinel
            await run_sentinel(session, source_type="paloalto", instance=fw)
    if fw.sync_nat:
        await _section("nat", lambda: sync_nat(session, fw, vsys_list))
    if fw.sync_addresses:
        await _section("addresses", lambda: sync_addresses(session, fw, vsys_list))

    fw.last_sync_at = datetime.now(UTC)
    fw.last_error = ("部分區段失敗：" + "；".join(f"{k}: {v}" for k, v in errors.items())
                     if errors else None)
    if errors:
        counts["errors"] = errors
    return counts
