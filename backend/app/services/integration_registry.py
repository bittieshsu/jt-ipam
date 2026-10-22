"""整合系統的共用清單（2026-10-21，戰情室「採集健康」第一個使用者）。

以前有三份各自維護的名單：同步腳本（scripts/jt-ipam-sync.py 的健康檢查，22 種）、系統診斷
（self_check._integration_errors，12 種）、/system/integration-presence。少列一個，那個整合壞掉時
畫面上就看不出來。這份與同步腳本那份一致（tests/test_integration_registry.py 守門），新整合兩邊都要加。

每個實例的狀態只用各整合本來就有的欄位：enabled、last_error、sync_interval_seconds，以及「最後成功」的時間。
最後成功不是每種都叫 last_sync_at：RustDesk 是代理主動回報（agent_last_seen_at／last_report_at），
ISOinsight 記的是最後完整成功（last_full_success_at）；只看 last_sync_at 會讓它們永遠是「尚未同步」（10-22）。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: (kind, 顯示名稱, 模組, 類別) —— 順序就是畫面上的順序
INTEGRATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("librenms", "LibreNMS", "app.models.librenms", "LibreNMSInstance"),
    ("wazuh", "Wazuh", "app.models.wazuh", "WazuhInstance"),
    ("zabbix", "Zabbix", "app.models.zabbix", "ZabbixInstance"),
    ("adguard", "AdGuard Home", "app.models.adguard", "AdGuardInstance"),
    ("proxmox", "Proxmox VE", "app.models.virt", "ProxmoxInstance"),
    ("esxi", "VMware", "app.models.esxi", "ESXiInstance"),
    ("hyperv", "Hyper-V", "app.models.hyperv", "HyperVInstance"),
    ("opnsense", "OPNsense", "app.models.firewall", "OPNsenseFirewall"),
    ("pfsense", "pfSense", "app.models.pfsense", "PfSenseFirewall"),
    ("fortigate", "FortiGate", "app.models.fortigate", "FortiGateFirewall"),
    ("paloalto", "Palo Alto", "app.models.paloalto", "PaloAltoFirewall"),
    ("checkpoint", "Check Point", "app.models.checkpoint", "CheckPointServer"),
    ("checkpoint_gaia", "Check Point Gaia", "app.models.checkpoint_gaia", "CheckPointGaiaTarget"),
    ("mikrotik", "MikroTik", "app.models.mikrotik", "MikroTikRouter"),
    ("windows_dhcp", "Windows DHCP", "app.models.windows_dhcp", "WindowsDhcpServer"),
    ("kea_dhcp", "Kea DHCP", "app.models.dhcp_standalone", "KeaDhcpServer"),
    ("isc_dhcp", "ISC DHCP", "app.models.dhcp_standalone", "IscDhcpServer"),
    ("isoinsight", "ISOinsight", "app.models.isoinsight", "IsoInsightSource"),
    ("technitium", "Technitium", "app.models.technitium", "TechnitiumDhcpServer"),
    ("rustdesk", "RustDesk", "app.models.rustdesk", "RustDeskServer"),
    ("ocs", "OCS Inventory", "app.models.ocs", "OcsServer"),
    ("dns", "DNS", "app.models.dns", "DNSServer"),
)

#: 看得出「最後成功」的欄位（有好幾個時取最新的）；每種整合至少要有一個（測試守門）
LAST_SUCCESS_FIELDS = ("last_sync_at", "agent_last_seen_at", "last_report_at", "last_full_success_at")

#: 沒有設定同步間隔的整合，最後同步超過這麼久算「逾時」
DEFAULT_STALE_AFTER = timedelta(hours=2)


def models() -> Iterator[tuple[str, str, Any]]:
    for kind, label, mod, cls in INTEGRATIONS:
        yield kind, label, getattr(importlib.import_module(mod), cls)


def last_success(row: Any) -> datetime | None:
    times = [t for f in LAST_SUCCESS_FIELDS if (t := getattr(row, f, None)) is not None]
    return max(times) if times else None


def state_of(row: Any, *, now: datetime) -> str:
    """ok／error／stale／never／disabled。逾時＝超過三個同步週期（或 DEFAULT_STALE_AFTER）沒有同步。"""
    if not getattr(row, "enabled", True):
        return "disabled"
    if getattr(row, "last_error", None):
        return "error"
    last = last_success(row)
    if last is None:
        return "never"
    interval = getattr(row, "sync_interval_seconds", None)
    limit = timedelta(seconds=int(interval) * 3) if interval else DEFAULT_STALE_AFTER
    return "stale" if now - last > limit else "ok"


async def instances(session: AsyncSession, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """每個整合實例一列：kind、label、id、name、state、last_sync_at、last_error（截短）。"""
    now = now or datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for kind, label, model in models():
        for row in (await session.execute(select(model))).scalars().all():
            out.append({
                "kind": kind, "label": label, "id": str(row.id),
                "name": str(getattr(row, "name", "") or label),
                "state": state_of(row, now=now),
                "last_sync_at": last_success(row),
                "last_error": (str(getattr(row, "last_error", "") or "")[:200]) or None,
            })
    return out
