"""IP 變更評估「舊位址仍有設備在用」要看防火牆的 ARP／VPN（使用者 2026-10-08：補）。

以前只看 last_seen_* 欄位（掃描代理、LibreNMS、Wazuh、Zabbix、OCS），防火牆同步寫在 `arp_seen`
的逐來源時間沒讀 —— 只有防火牆看得到的機器（沒裝代理、沒被監控）改址時就不會被提醒。
採信範圍跟上線判定一致：會過期的來源（ARP 表、目前連著的 VPN）才算；DHCP 租約不算。位址用 RFC 5737。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.test_change_impact_more_refs import _by_rule, _root, _run


async def _with_seen(db, seen: dict[str, datetime]):  # type: ignore[no-untyped-def]
    _sub, root = await _root(db)
    root.arp_seen = {k: v.isoformat() for k, v in seen.items()}
    await db.commit()
    return root


async def test_firewall_arp_counts_as_recent_activity(db_session, admin_user) -> None:
    root = await _with_seen(db_session, {"arp:opnsense": datetime.now(UTC) - timedelta(minutes=10)})
    res = await _run(db_session, admin_user, root)
    hits = _by_rule(res, "activity.old_ip_active")
    assert hits, "防火牆 ARP 表 10 分鐘前看到，應該提醒舊位址仍有設備在用"
    assert "arp:opnsense" in hits[0].params["sources"]


async def test_vpn_session_counts_too(db_session, admin_user) -> None:
    root = await _with_seen(db_session, {"vpn:fortigate": datetime.now(UTC) - timedelta(minutes=3)})
    res = await _run(db_session, admin_user, root)
    assert _by_rule(res, "activity.old_ip_active")


async def test_dhcp_lease_alone_is_not_activity(db_session, admin_user) -> None:
    """租約可能是幾天前拿的，不代表現在有人在用（與上線判定一致）。"""
    root = await _with_seen(db_session, {"lease:opnsense": datetime.now(UTC) - timedelta(minutes=5)})
    res = await _run(db_session, admin_user, root)
    assert not _by_rule(res, "activity.old_ip_active")


async def test_old_firewall_sightings_outside_the_window_do_not_count(db_session, admin_user) -> None:
    root = await _with_seen(db_session, {"arp:opnsense": datetime.now(UTC) - timedelta(days=60)})
    res = await _run(db_session, admin_user, root)
    assert not _by_rule(res, "activity.old_ip_active")
