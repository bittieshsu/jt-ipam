"""IP 變更評估：補齊既有功能裡的 IP 參照（2026-10-08 盤點）。

盤點所有存了位址／主機／網址的欄位後，評估漏讀的幾類：較新整合的連線端點（AdGuard、ISOinsight、
PVE/ESXi 的其他節點、OCS 資料庫、RustDesk 伺服器、掃描代理、Webhook）、jt-ipam 自己的系統設定
（LDAP、SMTP、AI 模型、稽核轉送）、線路的 IP／閘道／DNS、待審核的 IP 申請、IP 範圍、掃描代理看到的
DHCP 伺服器與它發給用戶端的預設閘道。位址一律用 RFC 5737。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.address import IPAddress
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from app.services.change_impact.config import DEFAULTS
from app.services.change_impact.engine import analyze

CFG: dict[str, Any] = {**DEFAULTS, "enabled": True}
OLD = "198.51.100.10"
NEW = "198.51.100.80"


async def _net(db) -> Subnet:  # type: ignore[no-untyped-def]
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    return sub


async def _root(db) -> tuple[Subnet, IPAddress]:  # type: ignore[no-untyped-def]
    sub = await _net(db)
    ip = IPAddress(subnet_id=sub.id, ip=OLD, state="active", hostname="erp")
    db.add(ip)
    await db.flush()
    return sub, ip


async def _user(db) -> User:  # type: ignore[no-untyped-def]
    u = User(username=f"u-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local", display_name="U",
             password_hash="x", auth_provider="local", is_active=True, is_admin=False)
    db.add(u)
    await db.flush()
    return u


async def _run(db, user, root: IPAddress, new_ip: str = NEW, target_subnet: Any = None):  # type: ignore[no-untyped-def]
    await db.commit()
    params: dict[str, Any] = {"new_ip": new_ip}
    if target_subnet is not None:
        params["target_subnet_id"] = str(target_subnet)
    return await analyze(db, user=user, scenario_type="ip_renumber", target_type="ip_address", target_id=root.id,
                         parameters=params, cfg=CFG)


def _by_rule(res, rule: str) -> list[Any]:  # type: ignore[no-untyped-def]
    return [f for f in res.findings if f.rule_id == rule]


def _gap_affected(res) -> set[str]:  # type: ignore[no-untyped-def]
    return {str(g.affected_analysis) for g in res.gaps if g.reason_code == "permission_limited"}


# ─────────────────── 整合與 jt-ipam 自己的連線端點 ───────────────────

async def test_newer_integration_endpoints_are_listed(db_session, admin_user) -> None:
    from app.models.adguard import AdGuardInstance
    from app.models.isoinsight import IsoInsightSource
    from app.models.notification import WebhookSubscription
    from app.models.ocs import OcsServer
    from app.models.rustdesk import RustDeskServer
    from app.models.scan_agent import ScanAgent
    from app.models.virt import ProxmoxInstance
    _sub, root = await _root(db_session)
    t = uuid.uuid4().hex[:6]
    db_session.add_all([
        AdGuardInstance(name=f"ag-{t}", api_url=f"https://{OLD}:3000", api_user="u", api_password_enc=b"x",
                        api_password_nonce=b"x"),
        IsoInsightSource(name=f"iso-{t}", base_url=f"http://{OLD}", username="u", password_enc=b"x",
                         password_nonce=b"x"),
        # 其他節點：換行或逗號分隔，主網址不是這個位址
        ProxmoxInstance(api_url="https://192.0.2.1:8006", auth_username="root@pam",
                        auth_token_id="t", extra_api_urls=f"https://192.0.2.2:8006, https://{OLD}:8006"),
        OcsServer(name=f"ocs-{t}", base_url="https://192.0.2.3", db_host=OLD),
        RustDeskServer(name=f"rd-{t}", hbbs_host=f"{OLD}:21116", relay_host=f"{OLD}:21117"),
        ScanAgent(name=f"agent-{t}", agent_url=f"https://{OLD}:8443"),
        WebhookSubscription(name=f"hook-{t}", target_url=f"https://{OLD}/hooks/ipam", secret_enc=b"x",
                            secret_nonce=b"x"),
    ])
    res = await _run(db_session, admin_user, root)
    got = {(f.params["kind"], f.params["field"]) for f in _by_rule(res, "config.integration_endpoint")}
    assert {("adguard", "api_url"), ("isoinsight", "base_url"), ("proxmox", "extra_api_urls"),
            ("ocs", "db_host"), ("rustdesk", "hbbs_host"), ("rustdesk", "relay_host"),
            ("scan_agent", "agent_url"), ("webhook", "target_url")} <= got
    # PVE 主網址不是這個位址，不可以因為其他節點有它就連主網址一起列
    assert ("proxmox", "api_url") not in got


async def test_extra_node_list_does_not_match_a_longer_address(db_session, admin_user) -> None:
    from app.models.virt import ProxmoxInstance
    _sub, root = await _root(db_session)
    db_session.add(ProxmoxInstance(api_url="https://192.0.2.1:8006", auth_username="root@pam",
                                   auth_token_id="t", extra_api_urls="https://198.51.100.100:8006"))
    res = await _run(db_session, admin_user, root)
    assert not _by_rule(res, "config.integration_endpoint")


async def test_system_settings_that_connect_to_the_address_are_listed(db_session, admin_user) -> None:
    from app.models.system_setting import SystemSetting
    from app.services import system_config
    db_session.add_all([
        SystemSetting(key="ldap", value={"enabled": True, "server": OLD}),
        SystemSetting(key="llm", value={"enabled": True, "url": f"http://{OLD}:11434"}),
        SystemSetting(key="notification_channels", value={"smtp_host": OLD}),
        SystemSetting(key="audit_forward", value={"enabled": True, "host": OLD, "port": 514}),
    ])
    _sub, root = await _root(db_session)
    await db_session.commit()
    # 這幾個設定有程序內快取，前面的測試可能留著舊值
    system_config._cache.clear()
    system_config._af_cache.clear()
    system_config._ncfg_cache.clear()
    res = await _run(db_session, admin_user, root)
    got = {f.params["setting"] for f in _by_rule(res, "config.system_endpoint")}
    assert {"ldap", "llm", "smtp", "audit_forward"} <= got
    system_config._cache.clear()
    system_config._af_cache.clear()
    system_config._ncfg_cache.clear()


async def test_admin_only_endpoints_are_a_gap_for_others(db_session) -> None:
    from app.models.adguard import AdGuardInstance
    from app.models.permission import Permission
    sub, root = await _root(db_session)
    db_session.add(AdGuardInstance(name="ag-y", api_url=f"https://{OLD}", api_user="u", api_password_enc=b"x",
                                   api_password_nonce=b"x"))
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=sub.id, principal_type="user", principal_id=u.id,
                              level="write"))
    res = await _run(db_session, u, root)
    assert not _by_rule(res, "config.integration_endpoint")
    assert not _by_rule(res, "config.system_endpoint")
    assert "integration_endpoint" in _gap_affected(res)


# ─────────────────── 線路 ───────────────────

async def test_circuit_address_gateway_and_dns_are_listed(db_session, admin_user) -> None:
    from app.models.advanced import Circuit, Provider
    _sub, root = await _root(db_session)
    p = Provider(name=f"isp-{uuid.uuid4().hex[:6]}")
    db_session.add(p)
    await db_session.flush()
    db_session.add_all([
        Circuit(cid="WAN-1", provider_id=p.id, ip_address=OLD),
        Circuit(cid="WAN-2", provider_id=p.id, gateway=OLD),
        Circuit(cid="WAN-3", provider_id=p.id, dns_servers=f"192.0.2.53, {OLD}"),
        Circuit(cid="WAN-4", provider_id=p.id, ip_address="198.51.100.100", dns_servers="198.51.100.101"),
    ])
    res = await _run(db_session, admin_user, root)
    got = {(f.subject_label, f.params["field"]) for f in _by_rule(res, "config.circuit_address")}
    assert got == {("WAN-1", "ip_address"), ("WAN-2", "gateway"), ("WAN-3", "dns_servers")}


async def test_circuits_need_global_read(db_session) -> None:
    from app.models.advanced import Circuit, Provider
    from app.models.permission import Permission
    sub, root = await _root(db_session)
    p = Provider(name=f"isp-{uuid.uuid4().hex[:6]}")
    db_session.add(p)
    await db_session.flush()
    db_session.add(Circuit(cid="WAN-1", provider_id=p.id, ip_address=OLD))
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=sub.id, principal_type="user", principal_id=u.id,
                              level="write"))
    res = await _run(db_session, u, root)
    assert not _by_rule(res, "config.circuit_address")
    assert "circuit" in _gap_affected(res)


# ─────────────────── 新位址：IP 申請、IP 範圍 ───────────────────

async def test_pending_ip_request_for_the_new_address(db_session, admin_user) -> None:
    from app.models.ip_request import IPRequest
    sub, root = await _root(db_session)
    db_session.add_all([
        IPRequest(requester_user_id=admin_user.id, subnet_id=sub.id, requested_ip=NEW, hostname="printer-3",
                  purpose="new printer", status="pending"),
        IPRequest(requester_user_id=admin_user.id, subnet_id=sub.id, requested_ip="198.51.100.81",
                  purpose="old", status="rejected"),
    ])
    res = await _run(db_session, admin_user, root)
    f = _by_rule(res, "ipam.new_ip_requested")
    assert len(f) == 1 and f[0].params["hostname"] == "printer-3"
    # 已駁回的申請不算
    res = await _run(db_session, admin_user, root, "198.51.100.81")
    assert not _by_rule(res, "ipam.new_ip_requested")


async def test_new_address_inside_an_ip_range(db_session, admin_user) -> None:
    from app.models.ip_range import IPRange
    sub, root = await _root(db_session)
    db_session.add(IPRange(subnet_id=sub.id, start_ip="198.51.100.70", end_ip="198.51.100.90", purpose="reserved",
                           name="printers"))
    res = await _run(db_session, admin_user, root)
    f = _by_rule(res, "ipam.new_ip_in_range")
    assert len(f) == 1 and f[0].params["purpose"] == "reserved" and f[0].params["range"] == "printers"
    res = await _run(db_session, admin_user, root, "198.51.100.91")
    assert not _by_rule(res, "ipam.new_ip_in_range")


# ─────────────────── 掃描代理看到的 DHCP ───────────────────

async def test_observed_dhcp_server_and_router(db_session, admin_user) -> None:
    from app.models.dhcp_sighting import DHCPSighting
    sub, root = await _root(db_session)
    now = datetime.now(UTC)
    db_session.add_all([
        DHCPSighting(subnet_id=sub.id, server_ip=OLD, router="198.51.100.1", first_seen_at=now - timedelta(days=3),
                     last_seen_at=now - timedelta(hours=1)),
        DHCPSighting(subnet_id=sub.id, server_ip="198.51.100.2", router=OLD, first_seen_at=now - timedelta(days=3),
                     last_seen_at=now - timedelta(hours=1)),
    ])
    res = await _run(db_session, admin_user, root)
    assert len(_by_rule(res, "dhcp.observed_server")) == 1
    r = _by_rule(res, "dhcp.observed_router")
    assert len(r) == 1 and r[0].params["server"] == "198.51.100.2"


async def test_old_dhcp_sightings_are_ignored(db_session, admin_user) -> None:
    from app.models.dhcp_sighting import DHCPSighting
    sub, root = await _root(db_session)
    then = datetime.now(UTC) - timedelta(days=40)
    db_session.add(DHCPSighting(subnet_id=sub.id, server_ip=OLD, router=OLD, first_seen_at=then, last_seen_at=then))
    res = await _run(db_session, admin_user, root)
    assert not _by_rule(res, "dhcp.observed_server") and not _by_rule(res, "dhcp.observed_router")


async def test_router_outside_the_sighting_subnet_is_not_a_gateway(db_session, admin_user) -> None:
    """預設閘道一定在用戶端的子網路裡：代理把別的子網路的 DHCP 回應算到這裡時，不可以說它是這裡的閘道。"""
    from app.models.dhcp_sighting import DHCPSighting
    sub, root = await _root(db_session)
    other = Subnet(section_id=sub.section_id, cidr="203.0.113.0/24")
    db_session.add(other)
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add(DHCPSighting(subnet_id=other.id, server_ip=OLD, router=OLD, first_seen_at=now - timedelta(days=1),
                                last_seen_at=now - timedelta(hours=1)))
    res = await _run(db_session, admin_user, root)
    assert not _by_rule(res, "dhcp.observed_router")
    assert len(_by_rule(res, "dhcp.observed_server")) == 1     # 發 DHCP 的伺服器可以在別的網段（中繼）


# ─────────────────── 服務（M2 的服務登錄，改址與除役也要看） ───────────────────

async def _service(db, name: str, members: list[tuple[str, Any, str]], required: int = 1,  # type: ignore[no-untyped-def]
                   endpoints: list[dict[str, Any]] | None = None) -> Any:
    from app.models.change_impact import (
        ImpactDependencyGroup,
        ImpactDependencyMember,
        ImpactService,
        ImpactServiceEndpoint,
    )
    svc = ImpactService(name=name, criticality="critical")
    db.add(svc)
    await db.flush()
    g = ImpactDependencyGroup(service_id=svc.id, name="app", required_count=required, position=0,
                              confirmed_at=datetime.now(UTC))
    db.add(g)
    await db.flush()
    for otype, oid, rel in members:
        db.add(ImpactDependencyMember(group_id=g.id, object_type=otype, object_id=oid, relation_type=rel))
    for e in endpoints or []:
        db.add(ImpactServiceEndpoint(service_id=svc.id, **e))
    await db.flush()
    return svc


async def _device(db, sub: Subnet, name: str, ip: str) -> tuple[Any, IPAddress]:  # type: ignore[no-untyped-def]
    from app.models.device import Device
    d = Device(name=name, type="server")
    db.add(d)
    await db.flush()
    a = IPAddress(subnet_id=sub.id, ip=ip, state="active", hostname=name, device_id=d.id)
    db.add(a)
    await db.flush()
    return d, a


async def _decommission(db, user, dev):  # type: ignore[no-untyped-def]
    await db.commit()
    return await analyze(db, user=user, scenario_type="device_decommission", target_type="device", target_id=dev.id,
                         parameters={}, cfg=CFG)


async def test_decommissioning_a_device_a_service_needs_is_a_disruption(db_session, admin_user) -> None:
    sub = await _net(db_session)
    app1, _ = await _device(db_session, sub, "app-1", "198.51.100.21")
    app2, _ = await _device(db_session, sub, "app-2", "198.51.100.22")
    await _service(db_session, "ERP", [("device", app1.id, "requires_network")])
    await _service(db_session, "CRM", [("device", app1.id, "requires_network"), ("device", app2.id, "requires_network")])
    res = await _decommission(db_session, admin_user, app1)
    hit = {f.subject_label: f.rule_id for f in res.findings if f.rule_id.startswith("service.")}
    assert hit == {"ERP": "service.modeled_disruption", "CRM": "service.dependencies_hold"}
    erp = next(f for f in res.findings if f.subject_label == "ERP")
    assert ("device:" + str(app1.id), "requires_network") in erp.links


async def test_service_registered_by_ip_counts_when_its_device_is_retired(db_session, admin_user) -> None:
    sub = await _net(db_session)
    dev, ip = await _device(db_session, sub, "db-1", "198.51.100.31")
    await _service(db_session, "ERP", [("ip", ip.id, "requires_network")])
    res = await _decommission(db_session, admin_user, dev)
    assert [f.rule_id for f in res.findings if f.subject_label == "ERP"] == ["service.modeled_disruption"]


async def test_decommission_without_any_service_modeled_is_not_a_gap(db_session, admin_user) -> None:
    sub = await _net(db_session)
    dev, _ = await _device(db_session, sub, "lonely", "198.51.100.41")
    res = await _decommission(db_session, admin_user, dev)
    assert "no_services_modeled" not in {g.reason_code for g in res.gaps}


async def test_renumbering_an_address_a_service_depends_on_or_publishes(db_session, admin_user) -> None:
    sub, root = await _root(db_session)
    await _service(db_session, "ERP", [("ip", root.id, "requires_network")])
    await _service(db_session, "Portal", [], endpoints=[{"hostname": f"{OLD}:443", "port": 443, "protocol": "tcp"}])
    await _service(db_session, "Report", [], endpoints=[{"object_type": "ip", "object_id": root.id, "port": 8443}])
    await _service(db_session, "Wiki", [], endpoints=[{"hostname": "198.51.100.100", "port": 80}])
    await _service(db_session, "Bare", [], endpoints=[{"hostname": OLD}])
    res = await _run(db_session, admin_user, root)
    assert {f.subject_label for f in _by_rule(res, "service.depends_on_address")} == {"ERP"}
    got = {f.subject_label: f.params["endpoint"] for f in _by_rule(res, "service.endpoint_address")}
    # 沒填埠號送代碼 any（前端翻成「未指定埠」）；198.51.100.100 不可以比中 198.51.100.10
    assert got == {"Portal": "tcp/443", "Report": "tcp/8443", "Bare": "any"}


async def test_services_need_global_read_in_renumbering_too(db_session) -> None:
    from app.models.permission import Permission
    sub, root = await _root(db_session)
    await _service(db_session, "ERP", [("ip", root.id, "requires_network")])
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=sub.id, principal_type="user", principal_id=u.id,
                              level="write"))
    res = await _run(db_session, u, root)
    assert not [f for f in res.findings if f.rule_id.startswith("service.")]
    assert "service" in _gap_affected(res)


# ─────────────────── Wazuh 代理的註冊位址 ───────────────────

async def test_wazuh_agent_registered_with_a_fixed_address(db_session, admin_user) -> None:
    """代理以固定位址註冊（不是 any）時，改址後管理端會拒絕它：要重新註冊。註冊成 any 的不用列。"""
    from app.models.wazuh import WazuhAgent, WazuhInstance
    _sub, root = await _root(db_session)
    inst = WazuhInstance(name=f"wz-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.5:55000", api_user="u",
                         api_password_enc=b"x", api_password_nonce=b"y")
    db_session.add(inst)
    await db_session.flush()
    db_session.add_all([
        WazuhAgent(instance_id=inst.id, agent_id="001", name="erp", jt_ipam_address_id=root.id, ip=OLD,
                   register_ip=OLD, status="active"),
        WazuhAgent(instance_id=inst.id, agent_id="002", name="erp-any", ip=OLD, register_ip=None, status="active"),
    ])
    res = await _run(db_session, admin_user, root)
    reg = _by_rule(res, "monitoring.wazuh_register_ip")
    assert [f.subject_label for f in reg] == ["erp"]
    assert {f.subject_label for f in _by_rule(res, "monitoring.wazuh_agent")} == {"erp", "erp-any"}


# ─────────────────── 新位址的 DHCP 保留／集區：整合範圍要看「目標子網路」 ───────────────────

async def _overlap_pair(db):  # type: ignore[no-untyped-def]
    """兩個單位都用 198.51.100.0/24：a 是根與目標，b 是另一個單位。"""
    a, root = await _root(db)
    b = await _net(db)
    return a, b, root


async def _fw(db, scope: list[Any] | None) -> Any:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    from app.models.firewall import OPNsenseFirewall
    fw = OPNsenseFirewall(name=f"fw-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.254", api_key_enc=b"x",
                          api_key_nonce=b"x", api_secret_enc=b"x", api_secret_nonce=b"x", enabled=True,
                          scope_subnet_ids=scope, last_sync_at=datetime.now(UTC), sync_dhcp=True)
    db.add(fw)
    await db.flush()
    return fw


async def test_new_ip_reservation_of_another_units_dhcp_is_not_a_blocker(db_session, admin_user) -> None:
    from app.models.dhcp import DHCPPoolRange, DHCPReservation
    a, b, root = await _overlap_pair(db_session)
    fw = await _fw(db_session, [b.id])
    db_session.add_all([
        DHCPReservation(source_type="opnsense", source_id=fw.id, ip=NEW, mac="00:00:5e:00:53:99", source="isc"),
        DHCPPoolRange(source_type="opnsense", source_id=fw.id, subnet_cidr="198.51.100.0/24",
                      start_ip="198.51.100.70", end_ip="198.51.100.90", source="isc"),
    ])
    res = await _run(db_session, admin_user, root, target_subnet=a.id)
    assert not _by_rule(res, "dhcp.new_ip_reserved"), "另一個單位的 DHCP 保留同一個位址，不可以擋這邊的改址"
    assert not _by_rule(res, "dhcp.new_ip_in_pool")


async def test_unscoped_dhcp_in_an_overlap_is_inferred_not_certain(db_session, admin_user) -> None:
    from app.models.dhcp import DHCPReservation
    a, _b, root = await _overlap_pair(db_session)
    fw = await _fw(db_session, None)
    db_session.add(DHCPReservation(source_type="opnsense", source_id=fw.id, ip=NEW, mac="00:00:5e:00:53:98",
                                   source="isc"))
    res = await _run(db_session, admin_user, root, target_subnet=a.id)
    f = _by_rule(res, "dhcp.new_ip_reserved")
    assert len(f) == 1 and f[0].strength == "inferred", ([x.rule_id for x in res.findings], sorted({g.reason_code for g in res.gaps}))
    assert "scope_unset_overlap" in {g.reason_code for g in res.gaps}
