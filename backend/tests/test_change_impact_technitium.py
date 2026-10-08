"""IP 變更評估 × Technitium（使用者 2026-10-08：新整合都要支援評估）。

- DHCP 範圍發給用戶端的預設閘道／DNS／NTP／WINS 是這個位址 → 改了它，整個範圍的用戶端都會斷
  （閘道：嚴重；DNS／NTP／WINS：高）。只看啟用中的範圍
- Technitium 的保留、發放範圍、租約走共用表，跟其他 DHCP 來源一樣被評估
- Technitium 主控台的網址就是這個位址 → 整合會斷（系統設定的連線位址）
- Technitium 當 DNS 伺服器時，紀錄走共用的 dns_records（同其他 DNS 類型）
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.dhcp import DHCPReservation
from app.models.technitium import TechnitiumDhcpScope, TechnitiumDhcpServer

from tests.test_change_impact_engine import _ip, _net, _rules, _run


async def _tdns(db, **kw) -> TechnitiumDhcpServer:  # type: ignore[no-untyped-def]
    inst = TechnitiumDhcpServer(name=f"tdns-{uuid.uuid4().hex[:4]}", api_url=kw.pop("api_url", "http://192.0.2.53:5380"),
                                enabled=True, last_sync_at=datetime.now(UTC), **kw)
    db.add(inst)
    await db.flush()
    return inst


async def test_gateway_handed_out_by_a_scope_is_critical(db_session, admin_user) -> None:
    sub = await _net(db_session)
    gw = await _ip(db_session, sub, "198.51.100.1")
    inst = await _tdns(db_session)
    db_session.add(TechnitiumDhcpScope(server_id=inst.id, name="lan", enabled=True, subnet_cidr="198.51.100.0/24",
                                       start_ip="198.51.100.100", end_ip="198.51.100.150", router="198.51.100.1",
                                       dns_servers=["198.51.100.2"]))
    db_session.add(TechnitiumDhcpScope(server_id=inst.id, name="off", enabled=False, subnet_cidr="198.51.100.0/24",
                                       start_ip="198.51.100.200", end_ip="198.51.100.210", router="198.51.100.1"))
    res = await _run(db_session, admin_user, gw, "198.51.100.254")
    found = [f for f in res.findings if f.rule_id == "dhcp.scope_router"]
    assert len(found) == 1, "停用的範圍不發位址，不算"
    f = found[0]
    assert f.rule.severity == "critical" and f.params["scope"] == "lan" and f.params["option"] == "router"
    assert f.params["source"] == inst.name


async def test_dns_server_handed_out_by_a_scope(db_session, admin_user) -> None:
    sub = await _net(db_session)
    dns = await _ip(db_session, sub, "198.51.100.2")
    inst = await _tdns(db_session)
    db_session.add(TechnitiumDhcpScope(server_id=inst.id, name="lan", enabled=True, subnet_cidr="198.51.100.0/24",
                                       start_ip="198.51.100.100", end_ip="198.51.100.150", router="198.51.100.1",
                                       dns_servers=["198.51.100.3", "198.51.100.2"], ntp_servers=["198.51.100.2"]))
    res = await _run(db_session, admin_user, dns, "198.51.100.253")
    opts = sorted(f.params["option"] for f in res.findings if f.rule_id == "dhcp.scope_option")
    assert opts == ["dns", "ntp"]
    assert all(f.rule.severity == "high" for f in res.findings if f.rule_id == "dhcp.scope_option")


async def test_dhcp_interface_address_of_a_scope(db_session, admin_user) -> None:
    sub = await _net(db_session)
    srv = await _ip(db_session, sub, "198.51.100.4")
    inst = await _tdns(db_session)
    db_session.add(TechnitiumDhcpScope(server_id=inst.id, name="lan", enabled=True, subnet_cidr="198.51.100.0/24",
                                       start_ip="198.51.100.100", end_ip="198.51.100.150", server_address="198.51.100.4"))
    res = await _run(db_session, admin_user, srv, "198.51.100.251")
    assert [f.params["option"] for f in res.findings if f.rule_id == "dhcp.scope_option"] == ["server"]


async def test_reservation_and_console_address_are_found(db_session, admin_user) -> None:
    sub = await _net(db_session)
    old = await _ip(db_session, sub, "198.51.100.53")
    inst = await _tdns(db_session, api_url="https://198.51.100.53:53443")
    db_session.add(DHCPReservation(source_type="technitium", source_id=inst.id, source_name=inst.name,
                                   ip="198.51.100.53", mac="02:00:5e:00:53:53"))
    res = await _run(db_session, admin_user, old, "198.51.100.252")
    rules = _rules(res)
    assert "dhcp.reservation" in rules
    assert any(r.startswith("config.") for r in rules), rules
    assert any(s["kind"] == "technitium" for s in res.manifest["sources"])
