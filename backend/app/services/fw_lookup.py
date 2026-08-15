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
