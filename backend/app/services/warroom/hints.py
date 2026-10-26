"""戰情室的 IP 判斷線索（使用者 10-22：未授權 IP「要順便顯示幾個可判斷資訊，一眼看推測可能是啥主機或裝置」）。

只用現成資料、不另外探測，每頁只查畫面上那幾筆：
- 異常報告裡的 MAC、廠牌（OUI）、哪些來源看到；本地管理（隨機）MAC 標 random_mac
- MAC 或 IP 對到虛擬化平台回報的 VM（fw_lookup.vm_match_for：對不到不代表是實體機）
- DNS 有名稱的 A／AAAA 紀錄指向這個 IP
- 推測的作業系統與設備類型（使用者 10-22）：有 IP 記錄的用設備類型／型號與有效 OS（依 OS 來源優先序）；
  沒有記錄的（未授權 IP 常見）用對到的 VM 回報的 OS，或還代表這個 IP 的 Wazuh 代理
廠牌、主機名稱都是外部回報的文字，前端只當純文字顯示。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values


def _report_macs(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """報告裡每個 IP 的第一筆 MAC 資訊（各類別都看；有廠牌的優先）。"""
    out: dict[str, dict[str, Any]] = {}
    for items in report.values():
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("ip"), str) or not it.get("macs"):
                continue
            macs = [m for m in it["macs"] if isinstance(m, dict) and m.get("mac")]
            if not macs:
                continue
            best = next((m for m in macs if m.get("vendor")), macs[0])
            cur = out.get(it["ip"])
            if cur is None or (not cur.get("vendor") and best.get("vendor")):
                out[it["ip"]] = best
    return out


async def _wazuh_os(session: AsyncSession, ip: str) -> str | None:
    """沒有 IP 記錄時：這個 IP 上連線中的 Wazuh 代理回報的作業系統（失聯的舊登記不採用）。"""
    from app.core.os_fingerprint import wazuh_os_display
    from app.models.wazuh import WazuhAgent
    for wa in (await session.execute(select(WazuhAgent).where(WazuhAgent.ip == ip).limit(5))).scalars().all():
        if (wa.status or "").lower() == "active":
            return wazuh_os_display(wa.os_name, wa.os_platform, wa.os_version)
    return None


async def ip_hints(session: AsyncSession, ips: list[str]) -> dict[str, dict[str, Any]]:
    ips = [i for i in dict.fromkeys(ips) if i][:20]
    if not ips:
        return {}
    from app.services.fw_lookup import vm_match_for
    from app.services.system_config import get_anomaly_report

    stored = await get_anomaly_report(session) or {}
    report = stored.get("report") if isinstance(stored.get("report"), dict) else stored
    macs = _report_macs(report if isinstance(report, dict) else {})
    from app.models.address import IPAddress
    from app.services.os_precedence import effective_os
    records: dict[str, Any] = {}
    for rec in (await session.execute(select(IPAddress).where(
            in_values(IPAddress.ip, ips)))).scalars().all():     # bounded: 最多 20 筆
        records.setdefault(str(rec.ip), rec)
    out: dict[str, dict[str, Any]] = {}
    for ip in ips:
        h: dict[str, Any] = {}
        m = macs.get(ip)
        if m:
            h["mac"] = str(m["mac"]).lower()
            if m.get("vendor"):
                h["vendor"] = str(m["vendor"])[:40]
            if m.get("local"):
                h["random_mac"] = True
            if m.get("sources"):
                h["sources"] = [str(s) for s in m["sources"]][:5]
        vm = await vm_match_for(session, ip=ip, macs=[h["mac"]] if h.get("mac") else None)
        if vm:
            h["vm"] = str(vm.get("vm") or "")[:60]
            if vm.get("platform"):
                h["platform"] = vm["platform"]
            if vm.get("os"):
                h["os"] = str(vm["os"])[:60]
            if vm.get("node"):
                h["node"] = str(vm["node"])[:60]
            if vm.get("cluster"):
                h["cluster"] = str(vm["cluster"])[:60]
        rec = records.get(ip)
        if rec is not None:
            if rec.device_kind:
                h["device_kind"] = rec.device_kind
                if rec.device_model:
                    h["device_model"] = str(rec.device_model)[:60]
            eff = await effective_os(session, rec)
            if eff.get("os_guess"):
                h["os"] = str(eff["os_guess"])[:60]        # IP 記錄的有效 OS 照優先序，蓋過 VM 回報的
        elif not h.get("os"):
            wos = await _wazuh_os(session, ip)
            if wos:
                h["os"] = wos[:60]
        if h:
            out[ip] = h
    # DNS：名稱指向這個 IP（一次查完）
    from app.models.dns import DNSRecord
    for name, value in (await session.execute(
            select(DNSRecord.name, DNSRecord.value).where(DNSRecord.type.in_(("A", "AAAA")),  # bounded: 兩種
                                                          in_values(DNSRecord.value, ips)))).all():
        out.setdefault(value, {}).setdefault("dns", str(name)[:80])
    return out


#: 接入埠最多看到幾個 MAC：超過就當成上聯埠或樹莓派一類的小交換器後面，寧可不寫也不要寫錯
ACCESS_PORT_MAX_MACS = 8


async def switch_port_of(session: AsyncSession, mac: str) -> dict[str, Any] | None:
    """這個 MAC 接在哪台交換器的哪個埠（戰情室 AI 情報的 WARNING 用，使用者 10-26）。

    MAC 會出現在路徑上每一台交換器的上聯埠，所以挑「看到的 MAC 最少」的那個埠當接入埠；
    連那個埠都看到很多 MAC（上聯、後面接了小交換器）就不寫。FDB 有 LibreNMS、MikroTik、SNMP 三種列，
    交換器名稱分別來自 LibreNMS 的 sysname、jt-ipam 裝置名稱、MikroTik 名稱、SNMP 目標對應的裝置。
    只給管理員看（WARNING 本身就只給完整權限的帳號）。
    """
    from sqlalchemy import func

    from app.models.device import Device
    from app.models.librenms import FDBEntry, LibreNMSDevice
    from app.models.mikrotik import MikroTikRouter
    from app.models.snmp import SnmpTarget

    keys = (FDBEntry.device_id, FDBEntry.switch_device_id, FDBEntry.mikrotik_router_id, FDBEntry.snmp_target_id)
    rows = (await session.execute(
        select(*keys, FDBEntry.port_name, FDBEntry.vlan_id_num, func.max(FDBEntry.last_seen_at))
        .where(FDBEntry.mac == mac, FDBEntry.port_name.is_not(None))
        .group_by(*keys, FDBEntry.port_name, FDBEntry.vlan_id_num)
        .order_by(func.max(FDBEntry.last_seen_at).desc()).limit(12))).all()   # bounded: 一個 MAC 最多看 12 個埠
    best: tuple[int, Any] | None = None
    for r in rows:
        dev, sw, ros, snmp, port = r[0], r[1], r[2], r[3], r[4]
        cond = [FDBEntry.port_name == port]
        for col, v in zip(keys, (dev, sw, ros, snmp), strict=True):
            cond.append(col == v if v is not None else col.is_(None))
        n = (await session.execute(select(func.count(func.distinct(FDBEntry.mac))).where(*cond))).scalar_one()
        if best is None or n < best[0]:
            best = (int(n), r)
    if best is None or best[0] > ACCESS_PORT_MAX_MACS:
        return None
    dev, sw, ros, snmp, port, vlan, _ = best[1]
    name = None
    if dev is not None:
        ld = await session.get(LibreNMSDevice, dev)
        name = (ld.sysname or ld.hostname) if ld else None
    elif sw is not None:
        d = await session.get(Device, sw)
        name = d.name if d else None
    elif ros is not None:
        rt = await session.get(MikroTikRouter, ros)
        name = rt.name if rt else None
    elif snmp is not None:
        t = await session.get(SnmpTarget, snmp)
        d = await session.get(Device, t.device_id) if t else None
        name = d.name if d else None
    if not name:
        return None
    return {"switch": str(name)[:60], "port": str(port)[:40], "vlan": vlan}
