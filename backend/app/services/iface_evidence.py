"""整合回報的設備介面位址＝上線證據 `iface:<整合>`（客戶 2026-10-14）。

同一台路由器／防火牆接好幾個 LAN 時，每個 LAN 的閘道都是它自己的一個介面位址。這些位址常被政策擋掉 ICMP、
或掃描代理不在那個網段，探測沒有回應 → 紅燈，實際上一直在用。整合本來就知道「這個位址設在我哪個介面上」：
這一輪 API 有回應、介面是啟用的，就在對得到的 IP 記錄蓋上時間（evidence.py 的 iface:<整合>，會過期）。
設備掛了同步失敗、不再蓋時間 → 證據過期 → 一起變紅。

只蓋已經存在的 IP 記錄、不自動建立：介面位址常包含 WAN、VPN、loopback，那些不一定該進 IPAM。
比對規則與防火牆的其他證據相同（fw_sightings.SightingBatch：唯一才算、照整合的子網路範圍）。
"""
from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


def _clean(raw: Any) -> str | None:
    """`192.0.2.1/24`、`192.0.2.1 255.255.255.0` → `192.0.2.1`；loopback、link-local、0.0.0.0 不算。"""
    text = str(raw or "").strip().split("/")[0].split()[0] if str(raw or "").strip() else ""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return None
    return str(ip)


def _dedupe(items: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    for x in items:
        if x and x not in out:
            out.append(x)
    return out


def _yes(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "yes", "1")


def opnsense_addrs(data: Any) -> list[str]:
    """`/api/interfaces/overview/export`：status 是 up、設定上沒有停用（enabled 不是 false）的介面。"""
    out: list[str | None] = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or str(it.get("status") or "").lower() != "up" or it.get("enabled") is False:
            continue
        for a in it.get("ipv4") or []:
            out.append(_clean(a.get("ipaddr") if isinstance(a, dict) else a))
        for a in it.get("ipv6") or []:
            out.append(_clean(a.get("ipaddr") if isinstance(a, dict) else a))
        if not it.get("ipv4") and it.get("addr4"):
            out.append(_clean(it.get("addr4")))
    return _dedupe(out)


def pfsense_addrs(data: Any) -> list[str]:
    """`/api/v2/status/interfaces`：只看 status（enable 欄位不可靠：實機 WAN 在用卻回 false）。
    ipaddr 是 dhcp／pppoe 這類字樣時不是位址，略過。"""
    rows = data.get("data") if isinstance(data, dict) else data
    out: list[str | None] = []
    for it in rows if isinstance(rows, list) else []:
        if not isinstance(it, dict) or str(it.get("status") or "").lower() != "up":
            continue
        out += [_clean(it.get("ipaddr")), _clean(it.get("ipaddrv6"))]
    return _dedupe(out)


def fortigate_addrs(results: Any) -> list[str]:
    """`/api/v2/cmdb/system/interface`：ip 是「位址 遮罩」、沒設時是 0.0.0.0；secondaryip 是附加位址。"""
    out: list[str | None] = []
    for it in results if isinstance(results, list) else []:
        if not isinstance(it, dict) or str(it.get("status") or "up").lower() != "up":
            continue
        out.append(_clean(it.get("ip")))
        for sec in it.get("secondaryip") or []:
            if isinstance(sec, dict):
                out.append(_clean(sec.get("ip")))
    return _dedupe(out)


def mikrotik_addrs(rows: Any) -> list[str]:
    """`/rest/ip/address`：RouterOS 全是字串；停用（disabled）與失效（invalid，介面不存在等）的不算。"""
    out: list[str | None] = []
    for it in rows if isinstance(rows, list) else []:
        if not isinstance(it, dict) or _yes(it.get("disabled")) or _yes(it.get("invalid")):
            continue
        out.append(_clean(it.get("address")))
    return _dedupe(out)


def librenms_addrs(addresses: Any, ports: Any, *, up_devices: set[int]) -> list[str]:
    """`/api/v0/resources/ip/addresses`（位址＋port_id）配 `/api/v0/ports`（port_id → device_id、埠狀態）：
    設備在 LibreNMS 是 up（不含停用）、而且那個埠是 up，位址才算。"""
    port_ok = {int(p["port_id"]): int(p["device_id"]) for p in ports if isinstance(p, dict)
               and p.get("port_id") is not None and p.get("device_id") is not None
               and str(p.get("ifOperStatus") or "").lower() == "up"}
    out: list[str | None] = []
    for a in addresses if isinstance(addresses, list) else []:
        if not isinstance(a, dict) or a.get("port_id") is None:
            continue
        dev = port_ok.get(int(a["port_id"]))
        if dev is not None and dev in up_devices:
            out.append(_clean(a.get("ipv4_address") or a.get("ipv6_address")))
    return _dedupe(out)


async def stamp(session: AsyncSession, source: str, addrs: Iterable[str], *, scope_ids: Any,
                instance: tuple[Any, str | None] | None = None) -> dict[str, int]:
    """對得到唯一 IP 記錄的蓋上 `arp_seen[source]`（現在）；不建新記錄。

    `instance`＝(整合 id, 名稱)：同時記下這台設備自己有哪些位址（integration_own_addresses），
    外部 DNS 才答得出「這個公網 IP 是哪台防火牆的」（2026-10-19）。"""
    from app.services.fw_sightings import SightingBatch
    addrs = list(addrs)
    batch = SightingBatch(session, source=source.split(":", 1)[-1], subnet_ids=scope_ids)
    seen = 0
    for ip in addrs:
        seen += 1
        batch.add(ip, evidence=source)
    matched = sum(1 for found, _e in await batch.flush() if found)
    if instance is not None:
        await record_own(session, source.split(":", 1)[-1], instance[0], instance[1], addrs)
    return {"seen": seen, "matched": matched}


async def record_own(session: AsyncSession, source: str, source_id: Any, name: str | None,
                     addrs: Iterable[str]) -> None:
    """這一輪完整讀到的自身位址：新的加入、還在的更新時間、不見的刪掉（只在讀取成功時呼叫）。"""
    from datetime import UTC, datetime

    from sqlalchemy import Text, delete, func
    from sqlalchemy.dialects.postgresql import insert

    from app.core.sqlin import not_in_values
    from app.models.external_dns import IntegrationOwnAddress
    want = sorted({a for a in (_clean(x) for x in addrs) if a})
    now = datetime.now(UTC)
    if want:
        stmt = insert(IntegrationOwnAddress).values([
            {"source_type": source, "source_id": source_id, "instance_name": name, "address": a, "last_seen_at": now}
            for a in want])
        await session.execute(stmt.on_conflict_do_update(
            constraint="uq_integration_own_addresses",
            set_={"last_seen_at": stmt.excluded.last_seen_at, "instance_name": stmt.excluded.instance_name}))
    q = delete(IntegrationOwnAddress).where(IntegrationOwnAddress.source_type == source,
                                            IntegrationOwnAddress.source_id == source_id)
    if want:
        q = q.where(not_in_values(func.host(IntegrationOwnAddress.address), want, type_=Text()))
    await session.execute(q)
