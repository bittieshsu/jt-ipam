"""Zabbix 同步（JSON-RPC，全程唯讀）。

**定位**：監控面補充，不是 LibreNMS 的替代。拿得到、也只承諾這四件事——
1. 主機↔IP 對應（host.get + selectInterfaces）→ 主機名稱來源之一
2. 存活狀態（available）→ `effective_status` 的第三個證據來源
3. 監控涵蓋落差（IPAM 有、Zabbix 沒有的位址）→ 比照 Wazuh 缺口
4. 維護狀態（maintenance）→ 讓維護中的主機不要被當成失聯而告警

ARP／FDB 不在 Zabbix 內建資料裡（要靠自訂 SNMP 項目，各站台設定不一），
因此**不承諾**，也不在 UI 上暗示有。

API 形態：POST 到 `/api_jsonrpc.php`，body 是 JSON-RPC；認證用 API token 走
`Authorization: Bearer`（5.4+），或以 user.login 換 session token（舊版）。
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import UnsafeOutboundURL, safe_request
from app.core.security import decrypt_secret, encrypt_secret
from app.models.address import IPAddress
from app.models.zabbix import ZabbixHost, ZabbixInstance


class ZabbixError(RuntimeError):
    pass


def _aad(instance_id: Any, field: str) -> bytes:
    return f"zabbix_instance:{instance_id}:{field}".encode()


def encrypt_token(instance_id: Any, raw: str) -> tuple[bytes, bytes]:
    return encrypt_secret(raw, aad=_aad(instance_id, "api_token"))


def encrypt_password(instance_id: Any, raw: str) -> tuple[bytes, bytes]:
    return encrypt_secret(raw, aad=_aad(instance_id, "api_password"))


def _decrypt(inst: ZabbixInstance, field: str) -> str | None:
    enc = getattr(inst, f"{field}_enc", None)
    nonce = getattr(inst, f"{field}_nonce", None)
    if not enc or not nonce:
        return None
    return decrypt_secret(enc, nonce, aad=_aad(inst.id, field)).decode("utf-8")


def _rpc_url(api_url: str) -> str:
    """接受填 `https://host` 或 `https://host/zabbix` 或完整的 api_jsonrpc.php。"""
    base = api_url.rstrip("/")
    return base if base.endswith("api_jsonrpc.php") else f"{base}/api_jsonrpc.php"


async def _rpc(
    inst: ZabbixInstance, method: str, params: Any, *,
    auth: str | None = None, timeout: float = 30.0,
) -> Any:
    """呼叫一個 JSON-RPC 方法。錯誤一律轉成可讀訊息（含 Zabbix 自己的 data 欄）。"""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    headers = {"Content-Type": "application/json-rpc"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    try:
        resp = await safe_request(
            "POST", _rpc_url(inst.api_url), json=payload, headers=headers,
            timeout=timeout, verify=inst.verify_tls,
        )
    except UnsafeOutboundURL as exc:
        raise ZabbixError(f"SSRF guard rejected URL: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ZabbixError(f"transport: {exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        raise ZabbixError(f"Zabbix {method}: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        # 與 FortiGate 那次同樣的教訓：訊息要帶證據，否則現場無從判斷
        snippet = " ".join(resp.text[:120].split())
        raise ZabbixError(
            f"Zabbix {method}: 回應不是 JSON（content-type="
            f"{resp.headers.get('content-type', '?')} 內容開頭={snippet!r}）"
            "——請確認網址是否指向 Zabbix 前端（會自動補 /api_jsonrpc.php）",
        ) from exc
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise ZabbixError(
            f"Zabbix {method}: {err.get('message')} {err.get('data', '')}".strip())
    return body.get("result") if isinstance(body, dict) else body


def _major(version: Any) -> int:
    """把 "6.0.28" 解析成 6；解析不出來時回 0（＝走最保守的舊版路徑）。"""
    try:
        return int(str(version).split(".", 1)[0])
    except (ValueError, TypeError):
        return 0


async def _auth_token(inst: ZabbixInstance, *, major: int | None = None) -> str:
    """優先用 API token；沒有就用帳密換 session token。

    `user.login` 的參數名在 5.4 從 `user` 改成 `username`（6.0 起只認 `username`）。
    版本未知時先用新的、失敗再退回舊的 —— 這種欄位改名不會有可讀錯誤，
    只會回「Invalid params」，現場很難判斷。
    """
    token = _decrypt(inst, "api_token")
    if token:
        return token
    pwd = _decrypt(inst, "api_password")
    if not inst.api_user or not pwd:
        raise ZabbixError("未設定 API token，也沒有帳號密碼")
    attempts = ["username"] if (major or 0) >= 6 else ["username", "user"]
    last: ZabbixError | None = None
    for key in attempts:
        try:
            result = await _rpc(inst, "user.login",
                                {key: inst.api_user, "password": pwd}, timeout=15.0)
        except ZabbixError as exc:
            last = exc
            continue
        if not isinstance(result, str):
            raise ZabbixError("user.login 沒有回傳 token")
        return result
    raise last or ZabbixError("user.login 失敗")


async def healthcheck(inst: ZabbixInstance) -> dict[str, Any]:
    """測試連線：回 Zabbix 版本與可讀的主機數（逐項回報，方便現場判斷）。"""
    version = await _rpc(inst, "apiinfo.version", {}, timeout=10.0)   # 不需認證
    out: dict[str, Any] = {"version": version}
    try:
        token = await _auth_token(inst, major=_major(version))
        hosts = await _rpc(inst, "host.get",
                           {"countOutput": True, "limit": 1}, auth=token, timeout=15.0)
        out["hosts_readable"] = True
        out["host_count"] = int(hosts) if isinstance(hosts, (int, str)) else None
    except ZabbixError as exc:
        out["hosts_readable"] = False
        out["error"] = str(exc)
    return out


def _scope_ids(inst: ZabbixInstance) -> set[Any]:
    out: set[Any] = set()
    for s in (inst.scope_subnet_ids or []):
        try:
            out.add(uuid.UUID(str(s)))
        except (ValueError, TypeError):
            pass
    return out


def _first_ip(interfaces: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """從介面清單挑一個位址。`useip=1` 者優先，否則回 DNS 名稱。"""
    ip, dns = None, None
    for i in interfaces or []:
        if ip is None and i.get("ip"):
            try:
                ipaddress.ip_address(str(i["ip"]).strip())
                ip = str(i["ip"]).strip()
            except ValueError:
                pass
        if dns is None and i.get("dns"):
            dns = str(i["dns"]).strip() or None
    return ip, dns


AVAILABLE_MAP = {"0": "unknown", "1": "up", "2": "down"}


def _availability(host: dict[str, Any]) -> str:
    """可用性：6.0+ 在介面上（取最樂觀的一個），舊版在主機上。

    多張介面時只要有一張是 up 就算 up —— 主機是通的，只是某個介面型別沒回應。
    """
    if "available" in host:
        return AVAILABLE_MAP.get(str(host.get("available")), "unknown")
    seen = {AVAILABLE_MAP.get(str(i.get("available")), "unknown")
            for i in (host.get("interfaces") or []) if "available" in i}
    for state in ("up", "down"):
        if state in seen:
            return state
    return "unknown"


async def sync_instance(session: AsyncSession, inst: ZabbixInstance) -> dict[str, Any]:
    """同步主機清單並對應到 IPAM。

    **只標既有 IP、不新建**（與其他整合一致）：Zabbix 的主機不一定是我們要管的位址，
    自動建立會讓「未授權 IP」那道訊號失效。
    """
    # 版本差異必須先問清楚，否則整批同步會直接失敗（不是少幾個欄位而已）：
    #   - `host.available` 5.4 起棄用、**6.0 移除** → 6.0+ 要從介面拿可用性
    #   - `selectGroups` 6.0 改名 `selectHostGroups`，7.0 移除舊名
    version = await _rpc(inst, "apiinfo.version", {}, timeout=10.0)
    major = _major(version)
    token = await _auth_token(inst, major=major)

    output = ["hostid", "host", "name", "status", "maintenance_status"]
    iface = ["ip", "dns", "useip", "type", "main"]
    params: dict[str, Any] = {
        "selectTags": ["tag", "value"],
        "selectInventory": ["macaddress_a", "os", "location", "serialno_a", "oob_ip"],
    }
    if major >= 6:
        iface.append("available")
        params["selectHostGroups"] = ["name"]
    else:
        output.append("available")
        params["selectGroups"] = ["name"]
    params["output"] = output
    params["selectInterfaces"] = iface

    hosts = await _rpc(inst, "host.get", params, auth=token, timeout=60.0)
    if not isinstance(hosts, list):
        raise ZabbixError("host.get 回傳格式非預期")

    scope = _scope_ids(inst)
    now = datetime.now(UTC)
    seen: set[str] = set()
    linked = 0

    for h in hosts:
        hostid = str(h.get("hostid") or "")
        if not hostid:
            continue
        seen.add(hostid)
        ip, dns = _first_ip(h.get("interfaces") or [])

        # 重疊網段：一定要 scope + limit(1)，否則 MultipleResultsFound 會炸掉整批
        addr_id = None
        if ip:
            stmt = select(IPAddress).where(IPAddress.ip == ip)
            if scope:
                stmt = stmt.where(IPAddress.subnet_id.in_(scope))
            ipa = (await session.execute(stmt.limit(1))).scalars().first()
            if ipa is not None:
                addr_id = ipa.id
                linked += 1
                # 主機名稱觀測（來源 zabbix，依全域優先序決定是否採用）
                from app.services.hostname import apply_observation
                name = (h.get("name") or h.get("host") or "").strip()
                if name:
                    # 多台 Zabbix 主機可能指向同一 IP → tiebreak 穩定收斂
                    # （Wazuh 就是漏了這個，十天洗出 620 筆翻動）
                    await apply_observation(session, ip=ipa, source="zabbix",
                                            hostname=name, tiebreak_min=True)

        existing = (await session.execute(
            select(ZabbixHost).where(ZabbixHost.instance_id == inst.id,
                                     ZabbixHost.hostid == hostid))).scalars().first()
        values = {
            "host": str(h.get("host") or "")[:255],
            "name": str(h.get("name") or "")[:255] or None,
            "status": "monitored" if str(h.get("status")) == "0" else "unmonitored",
            "available": _availability(h),
            "maintenance": str(h.get("maintenance_status") or "0") == "1",
            "ip": ip, "dns": dns,
            "groups": [g.get("name") for g in (h.get("hostgroups") or h.get("groups") or [])],
            "tags": h.get("tags") or [],
            "inventory": h.get("inventory") or None,
            "jt_ipam_address_id": addr_id,
            "last_seen_at": now,
            "synced_at": now,
        }
        if existing is None:
            session.add(ZabbixHost(instance_id=inst.id, hostid=hostid, **values))
        else:
            for k, v in values.items():
                setattr(existing, k, v)

    # 移除已不存在於 Zabbix 的主機（鏡像資料，不保留幽靈）
    stale = (await session.execute(
        select(ZabbixHost).where(ZabbixHost.instance_id == inst.id))).scalars().all()
    removed = 0
    for row in stale:
        if row.hostid not in seen:
            await session.delete(row)
            removed += 1

    inst.last_sync_at = now
    inst.last_error = None
    return {"hosts": len(hosts), "linked": linked, "removed": removed}


async def coverage_gap(
    session: AsyncSession, *, instance_id: uuid.UUID | None = None,
    subnet_ids: list[uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """有主機名稱、卻沒有被 Zabbix 監控的 IP（監控涵蓋缺口）。

    與 Wazuh 的代理缺口同一個模式；`subnet_ids` 必給才不會用全站資料回答某網段的問題。
    """
    sub = select(ZabbixHost.jt_ipam_address_id).where(
        ZabbixHost.jt_ipam_address_id.is_not(None),
        ZabbixHost.status == "monitored")
    if instance_id is not None:
        sub = sub.where(ZabbixHost.instance_id == instance_id)
    stmt = select(IPAddress.id, IPAddress.ip, IPAddress.hostname).where(
        IPAddress.id.not_in(sub),
        IPAddress.hostname.is_not(None), IPAddress.hostname != "")
    if subnet_ids is not None:
        if not subnet_ids:
            return []
        stmt = stmt.where(IPAddress.subnet_id.in_(subnet_ids))
    rows = (await session.execute(stmt)).all()
    return [{"ip_address_id": str(i), "ip": str(ip).split("/", 1)[0] if ip else None,
             "hostname": hn} for i, ip, hn in rows]
