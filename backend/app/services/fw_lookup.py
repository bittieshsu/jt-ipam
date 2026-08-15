"""「這個 IP 被哪些防火牆規則管到」—— 反向查詢。

日常維運最常問的是反向問題：不是「這條規則管誰」，而是「這台機器對外開了什麼、
誰能連它」。這裡把三家防火牆的規則、NAT 與別名做成以 IP 為中心的反查。

比對語意（刻意保守、每筆附命中原因）：
- **明確命中**才列：規則欄位是這個 IP、或是包含它的 CIDR、或是「成員包含它的別名」。
- `any` 不列 —— 每條 any 規則都命中每個 IP，列了等於整頁雜訊；UI 另以一句話註明
  「來源/目的為 any 的規則也適用」。
- 全部確定性查詢；別名成員只認單一 IP 與 CIDR（URL 型別名內容不可判定，跳過）。
"""
from __future__ import annotations

import ipaddress
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _field_matches(field: Any, aip: Any, alias_names: set[str]) -> str | None:
    """規則欄位是否明確涵蓋這個 IP。回傳命中原因；None＝不命中（含 any）。"""
    if field is None:
        return None
    if isinstance(field, dict):
        # pfSense 的 source/destination 物件：{"address": "..."} / {"network": "..."}
        for v in field.values():
            r = _field_matches(v, aip, alias_names)
            if r:
                return r
        return None
    text = str(field).strip()
    if not text or text.lower() in ("any", "*", "all"):
        return None
    if text in alias_names:
        return f"別名 {text} 的成員"
    host = text.split("/")[0]
    try:
        if "/" in text:
            if aip in ipaddress.ip_network(text, strict=False):
                return f"網段 {text} 涵蓋"
            return None
        if ipaddress.ip_address(host) == aip:
            return "位址完全相符"
    except ValueError:
        return None
    return None


def _member_covers(members: list | None, aip: Any) -> bool:
    for m in members or []:
        t = str(m).strip()
        try:
            if "/" in t:
                if aip in ipaddress.ip_network(t, strict=False):
                    return True
            elif ipaddress.ip_address(t) == aip:
                return True
        except ValueError:
            continue
    return False


async def rules_touching_ip(session: AsyncSession, ip: str) -> dict[str, Any]:
    """回傳 {rules, nat, aliases}，每筆附 source_type／防火牆名／命中原因。"""
    from app.models.address import IPAddress
    from app.models.firewall import OPNsenseFirewall, OPNsenseSyncedAlias
    from app.models.firewall_rule import OPNsenseRule
    from app.models.fortigate import FortiGateFirewall, FortiGatePolicy
    from app.models.nat import NATTranslation
    from app.models.pfsense import PfSenseFirewall, PfSenseSyncedAlias

    aip = ipaddress.ip_address(ip)
    out: dict[str, Any] = {"rules": [], "nat": [], "aliases": []}

    # ── 別名：這個 IP 在哪些別名裡（其名字之後也拿來比對規則欄位）──
    alias_names: set[str] = set()
    for alias in (await session.execute(select(OPNsenseSyncedAlias))).scalars().all():
        if _member_covers(alias.content, aip):
            alias_names.add(alias.name)
            out["aliases"].append({"source_type": "opnsense", "name": alias.name,
                                   "descr": (alias.description or "")[:120]})
    for alias in (await session.execute(select(PfSenseSyncedAlias))).scalars().all():
        if _member_covers(alias.members, aip):
            alias_names.add(alias.name)
            out["aliases"].append({"source_type": "pfsense", "name": alias.name,
                                   "descr": (alias.descr or "")[:120]})

    # ── OPNsense 規則 ──
    opn_names = {f.id: f.name for f in (await session.execute(
        select(OPNsenseFirewall))).scalars().all()}
    for r in (await session.execute(
            select(OPNsenseRule).where(OPNsenseRule.enabled.is_(True)))).scalars().all():
        why_src = _field_matches(r.source_net, aip, alias_names)
        why_dst = _field_matches(r.destination_net, aip, alias_names)
        if why_src or why_dst:
            out["rules"].append({
                "source_type": "opnsense", "firewall": opn_names.get(r.firewall_id, "?"),
                "action": r.action, "interface": r.interface, "protocol": r.protocol,
                "src": str(r.source_net or "any"), "dst": str(r.destination_net or "any"),
                "dst_port": str(getattr(r, "destination_port", "") or ""),
                "descr": (r.description or "")[:120],
                "match": ("目的：" + why_dst) if why_dst else ("來源：" + why_src),
            })

    # ── pfSense 規則（JSONB）──
    for fw in (await session.execute(
            select(PfSenseFirewall).where(PfSenseFirewall.rules.is_not(None)))).scalars().all():
        for r in (fw.rules or []):
            if not isinstance(r, dict) or r.get("disabled"):
                continue
            why_src = _field_matches(r.get("source"), aip, alias_names)
            why_dst = _field_matches(r.get("destination"), aip, alias_names)
            if why_src or why_dst:
                out["rules"].append({
                    "source_type": "pfsense", "firewall": fw.name,
                    "action": r.get("type"), "interface": r.get("interface"),
                    "protocol": r.get("protocol"),
                    "src": str(r.get("source") or "any"), "dst": str(r.get("destination") or "any"),
                    "dst_port": str(r.get("destination_port") or ""),
                    "descr": (r.get("descr") or "")[:120],
                    "match": ("目的：" + why_dst) if why_dst else ("來源：" + why_src),
                })

    # ── FortiGate 政策 ──
    fg_names = {f.id: f.name for f in (await session.execute(
        select(FortiGateFirewall))).scalars().all()}
    for r in (await session.execute(
            select(FortiGatePolicy))).scalars().all():
        if (r.status or "") == "disable":
            continue
        why_src = _field_matches(getattr(r, "srcaddr", None), aip, alias_names)
        why_dst = _field_matches(getattr(r, "dstaddr", None), aip, alias_names)
        if why_src or why_dst:
            out["rules"].append({
                "source_type": "fortigate", "firewall": fg_names.get(r.firewall_id, "?"),
                "action": r.action, "interface": f"{r.srcintf}->{getattr(r, 'dstintf', '')}",
                "protocol": str(getattr(r, "service", "") or ""),
                "src": str(getattr(r, "srcaddr", "") or ""), "dst": str(getattr(r, "dstaddr", "") or ""),
                "dst_port": "", "descr": (r.name or "")[:120],
                "match": ("目的：" + why_dst) if why_dst else ("來源：" + why_src),
            })

    # ── NAT：指向（或來自）這個 IP 的對應 ──
    ipa = (await session.execute(
        select(IPAddress).where(IPAddress.ip == ip).limit(1))).scalars().first()
    if ipa is not None:
        for n in (await session.execute(
                select(NATTranslation).where(
                    (NATTranslation.dst_ip_id == ipa.id) | (NATTranslation.src_ip_id == ipa.id),
                    NATTranslation.disabled.is_(False)).limit(50))).scalars().all():
            out["nat"].append({
                "name": n.name, "type": n.type, "protocol": n.protocol,
                "dst_port": n.dst_port, "src_port": n.src_port,
                "source": (n.source_origin or "manual").split(":")[0],
                "descr": (n.description or "")[:120],
            })

    return out


async def attack_surface(session: AsyncSession) -> list[dict[str, Any]]:
    """對外攻擊面清單：從外面可達的 IP:port，每項配 IPAM 身分。

    異常偵測的「對外曝險」是抓問題；這裡是**盤點** —— 資安稽核第一個要的東西。
    保守原則同規則腐化：**只列明確可判定的**——
    - NAT port forward（生效中）：目標經 dst_ip_id 連結，或本來就懸空（那本身是紅旗）。
    - WAN 介面上、目的為單一 IP 的放行規則（pfSense JSONB＋OPNsense 規則表）。
    - 目的是別名／any／網段的規則**不列**：展開猜測會產生假清單，假清單比沒有更危險
      （稽核拿去簽名的東西不能有猜的成分）。FortiGate 欄位語意逐家驗證後再納入。
    """
    from app.models.address import IPAddress
    from app.models.customer import Customer
    from app.models.firewall import OPNsenseFirewall
    from app.models.firewall_rule import OPNsenseRule
    from app.models.nat import NATTranslation
    from app.models.pfsense import PfSenseFirewall
    from app.models.subnet import Subnet
    from app.models.wazuh import WazuhAgent

    items: list[dict[str, Any]] = []

    async def identity(ip_str: str | None, ip_id: Any = None) -> dict[str, Any]:
        """目標的 IPAM 身分。未登錄不是省略，是紅旗 —— 對外開口指向不明主機。"""
        ipa = None
        if ip_id is not None:
            ipa = await session.get(IPAddress, ip_id)
        elif ip_str:
            ipa = (await session.execute(
                select(IPAddress).where(IPAddress.ip == ip_str).limit(1))).scalars().first()
        if ipa is None:
            return {"registered": False}
        sub = await session.get(Subnet, ipa.subnet_id)
        cust = (await session.get(Customer, sub.customer_id)) if (sub and sub.customer_id) else None
        wa = (await session.execute(
            select(WazuhAgent).where(WazuhAgent.ip == str(ipa.ip)).limit(1))).scalars().first()
        return {
            "registered": True, "ip": str(ipa.ip),
            "hostname": ipa.hostname, "status": ipa.effective_status,
            "subnet": str(sub.cidr) if sub else None,
            "customer": cust.name if cust else None,
            "wazuh": None if wa is None else (wa.status or "present"),
        }

    # ── NAT port forwards（三家共用的正規化表）──
    from app.models.fortigate import FortiGateFirewall
    inst_names: dict[str, str] = {}
    for model in (OPNsenseFirewall, PfSenseFirewall, FortiGateFirewall):
        for f in (await session.execute(select(model))).scalars().all():
            inst_names[str(f.id)] = f.name
    for n in (await session.execute(
            select(NATTranslation).where(
                NATTranslation.type == "port_forward",
                NATTranslation.disabled.is_(False)).limit(300))).scalars().all():
        ident = await identity(None, n.dst_ip_id) if n.dst_ip_id else {"registered": False}
        origin = (n.source_origin or "manual").split(":")
        items.append({
            "via": "nat", "source": origin[0],
            "firewall": inst_names.get(origin[1]) if len(origin) > 1 else None,
            "name": n.name, "protocol": n.protocol,
            "port": n.dst_port, "descr": (n.description or "")[:120],
            "identity": ident,
        })

    # ── WAN 放行規則、目的為單一 IP ──
    def _single_ip(v: Any) -> str | None:
        if isinstance(v, dict):
            for x in v.values():
                r = _single_ip(x)
                if r:
                    return r
            return None
        t = str(v or "").strip()
        if not t or "/" in t or t.lower() in ("any", "*", "all"):
            return None
        try:
            ipaddress.ip_address(t)
        except ValueError:
            return None
        return t

    for fw in (await session.execute(
            select(PfSenseFirewall).where(PfSenseFirewall.rules.is_not(None)))).scalars().all():
        for r in (fw.rules or []):
            if not isinstance(r, dict) or r.get("disabled"):
                continue
            if str(r.get("type") or "").lower() not in ("pass", "match"):
                continue
            if "wan" not in str(r.get("interface") or "").lower():
                continue
            target = _single_ip(r.get("destination"))
            if not target:
                continue
            items.append({
                "via": "rule", "source": "pfsense", "firewall": fw.name,
                "name": (r.get("descr") or str(r.get("tracker") or ""))[:120],
                "protocol": r.get("protocol"), "port": str(r.get("destination_port") or ""),
                "descr": (r.get("descr") or "")[:120],
                "identity": await identity(target),
            })

    opn_names = {f.id: f.name for f in (await session.execute(
        select(OPNsenseFirewall))).scalars().all()}
    for r in (await session.execute(
            select(OPNsenseRule).where(OPNsenseRule.enabled.is_(True)))).scalars().all():
        if "wan" not in str(r.interface or "").lower():
            continue
        if str(r.action or "").lower() not in ("pass", "accept"):
            continue
        target = _single_ip(r.destination_net)
        if not target:
            continue
        items.append({
            "via": "rule", "source": "opnsense",
            "firewall": opn_names.get(r.firewall_id, "?"),
            "name": (r.description or "")[:120],
            "protocol": r.protocol, "port": str(getattr(r, "destination_port", "") or ""),
            "descr": (r.description or "")[:120],
            "identity": await identity(target),
        })

    return items[:500]
