"""Technitium 的 DHCP（獨立整合，比照 Kea）：範圍→發放範圍、保留→DHCP 保留、租約→既有 IP 的租約／MAC／主機名稱，
另存一份範圍鏡像（閘道／DNS／NTP／WINS，改址評估用）。

回應形狀照實機 15.6（測試用容器，busybox udhcpc 真的拿到租約）錄下來：
- scopes/list 只有摘要；保留、選項、排除區間要逐個範圍 scopes/get
- 停用的範圍不發位址：不寫發放範圍、保留與選項，鏡像照留（標停用）
- 排除區間要從發放範圍挖掉（Default 範圍 .1–.254 排除 .1–.10 → 實際發 .11–.254）
- 租約的時間是 UTC（帶 Z）；過期的不算；主機名稱是 FQDN
- 讀不到 DHCP（權限）＝整次失敗、寫 last_error，不清任何東西
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.address import IPAddress
from app.models.dhcp import DHCPPoolRange, DHCPReservation
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.technitium import TechnitiumDhcpScope, TechnitiumDhcpServer
from app.services import technitium as tc
from app.services import technitium_dhcp as td
from sqlalchemy import select


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


SCOPES = {"scopes": [
    {"name": "Default", "enabled": False, "startingAddress": "203.0.113.1", "endingAddress": "203.0.113.254",
     "subnetMask": "255.255.255.0", "networkAddress": "203.0.113.0"},
    {"name": "lan", "enabled": True, "startingAddress": "172.31.250.100", "endingAddress": "172.31.250.150",
     "subnetMask": "255.255.255.0", "networkAddress": "172.31.250.0", "interfaceAddress": "172.31.250.2"},
]}
SCOPE = {
    "lan": {"name": "lan", "startingAddress": "172.31.250.100", "endingAddress": "172.31.250.150",
            "subnetMask": "255.255.255.0", "leaseTimeDays": 0, "leaseTimeHours": 1, "leaseTimeMinutes": 0,
            "domainName": "example.test", "routerAddress": "172.31.250.1", "useThisDnsServer": False,
            "dnsServers": ["172.31.250.2"], "ntpServers": ["172.31.250.3"], "winsServers": None,
            "exclusions": [{"startingAddress": "172.31.250.110", "endingAddress": "172.31.250.119"}],
            "reservedLeases": [{"hostName": "printer-01", "hardwareAddress": "02-00-5E-00-53-01",
                                "address": "172.31.250.120", "comments": "e2e"}]},
    "Default": {"name": "Default", "startingAddress": "203.0.113.1", "endingAddress": "203.0.113.254",
                "subnetMask": "255.255.255.0", "routerAddress": "203.0.113.1", "dnsServers": ["172.31.250.2"],
                "exclusions": [{"startingAddress": "203.0.113.1", "endingAddress": "203.0.113.10"}],
                "reservedLeases": [{"hostName": "x", "hardwareAddress": "02-00-5E-00-53-09", "address": "203.0.113.9"}]},
}
LEASES = {"leases": [
    {"scope": "lan", "type": "Dynamic", "hardwareAddress": "02-00-5E-00-53-21", "address": "172.31.250.100",
     "hostName": "laptop-01.example.test", "leaseObtained": _iso(-timedelta(minutes=5)),
     "leaseExpires": _iso(timedelta(minutes=55))},
    {"scope": "lan", "type": "Reserved", "hardwareAddress": "02-00-5E-00-53-01", "address": "172.31.250.120",
     "hostName": "printer-01.example.test", "leaseObtained": _iso(-timedelta(hours=1)),
     "leaseExpires": _iso(timedelta(hours=2))},
    {"scope": "lan", "type": "Dynamic", "hardwareAddress": "02-00-5E-00-53-22", "address": "172.31.250.101",
     "hostName": None, "leaseObtained": _iso(-timedelta(hours=3)), "leaseExpires": _iso(-timedelta(hours=2))},
]}


def test_pool_bounds_subtract_exclusions() -> None:
    assert td.pool_ranges("172.31.250.100", "172.31.250.150",
                          [{"startingAddress": "172.31.250.110", "endingAddress": "172.31.250.119"}]) == [
        ("172.31.250.100", "172.31.250.109"), ("172.31.250.120", "172.31.250.150")]
    assert td.pool_ranges("203.0.113.1", "203.0.113.254",
                          [{"startingAddress": "203.0.113.1", "endingAddress": "203.0.113.10"}]) == [
        ("203.0.113.11", "203.0.113.254")]
    # 排除整段、排除超出範圍、壞資料：不讓整批失敗
    assert td.pool_ranges("10.0.0.10", "10.0.0.20", [{"startingAddress": "10.0.0.1", "endingAddress": "10.0.0.30"}]) == []
    assert td.pool_ranges("10.0.0.10", "10.0.0.20", [{"startingAddress": "x"}]) == [("10.0.0.10", "10.0.0.20")]


def test_scope_parsing() -> None:
    sc = td.parse_scope(SCOPE["lan"], enabled=True)
    assert sc["subnet"] == "172.31.250.0/24" and sc["router"] == "172.31.250.1"
    assert sc["dns_servers"] == ["172.31.250.2"] and sc["ntp_servers"] == ["172.31.250.3"] and sc["wins_servers"] == []
    assert sc["lease_seconds"] == 3600 and sc["domain_name"] == "example.test"
    assert sc["reservations"] == [{"ip": "172.31.250.120", "mac": "02:00:5e:00:53:01", "hostname": "printer-01",
                                   "subnet": "172.31.250.0/24"}]


def test_leases_skip_expired_and_normalise() -> None:
    out = td.parse_leases(LEASES["leases"])
    assert [(x["ip"], x["mac"], x["hostname"]) for x in out] == [
        ("172.31.250.100", "02:00:5e:00:53:21", "laptop-01.example.test"),
        ("172.31.250.120", "02:00:5e:00:53:01", "printer-01.example.test")]
    assert out[0]["ends"].startswith(str(datetime.now(UTC).year))


def _fake(monkeypatch, *, denied: bool = False, calls: list | None = None) -> None:
    async def call(self, path, **params):  # type: ignore[no-untyped-def]
        if calls is not None:
            calls.append((path, params))
        if denied:
            raise tc.TechnitiumError(f"{path}: access was denied", code="technitium_denied", endpoint=path)
        if path == "/api/user/session/get":
            return {"username": "jtipam-ro", "info": {"version": "15.6", "dnsServerDomain": "tdns", "permissions": {
                "DhcpServer": {"canView": True, "canModify": False, "canDelete": False}}}}
        if path == "/api/dhcp/scopes/list":
            return SCOPES
        if path == "/api/dhcp/scopes/get":
            return SCOPE[params["name"]]
        if path == "/api/dhcp/leases/list":
            return LEASES
        raise AssertionError(path)
    monkeypatch.setattr(tc.TechnitiumClient, "call", call)


async def _net(db) -> dict[str, IPAddress]:  # type: ignore[no-untyped-def]
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="172.31.250.0/24")
    db.add(sub)
    await db.flush()
    ips = {}
    for last in (100, 101, 120):
        ip = IPAddress(subnet_id=sub.id, ip=f"172.31.250.{last}", state="active")
        db.add(ip)
        ips[str(last)] = ip
    await db.commit()
    return ips


async def _inst(db, **kw) -> TechnitiumDhcpServer:  # type: ignore[no-untyped-def]
    inst = TechnitiumDhcpServer(name=f"tdns-{uuid.uuid4().hex[:6]}", api_url="http://192.0.2.53:5380", **kw)
    db.add(inst)
    await db.flush()
    inst.token_enc, inst.token_nonce = td.encrypt_token(inst.id, "tok-abc")
    await db.commit()
    return inst


async def test_sync_writes_pools_reservations_leases_and_scope_options(db_session, monkeypatch) -> None:
    ips = await _net(db_session)
    _fake(monkeypatch)
    inst = await _inst(db_session)
    summary = await td.sync_instance(db_session, inst)
    await db_session.commit()
    assert summary == {"scopes": 2, "pools": 2, "reservations": 1, "leases": 2}

    pools = (await db_session.execute(select(DHCPPoolRange).where(DHCPPoolRange.source_id == inst.id)
                                      .order_by(DHCPPoolRange.start_ip))).scalars().all()
    assert [(p.source_type, p.source, p.start_ip, p.end_ip) for p in pools] == [
        ("technitium", "technitium", "172.31.250.100", "172.31.250.109"),
        ("technitium", "technitium", "172.31.250.120", "172.31.250.150")]
    res = (await db_session.execute(select(DHCPReservation).where(DHCPReservation.source_id == inst.id))).scalar_one()
    assert (res.ip, res.mac, res.hostname) == ("172.31.250.120", "02:00:5e:00:53:01", "printer-01")

    ip100 = await db_session.get(IPAddress, ips["100"].id)
    await db_session.refresh(ip100)
    assert ip100.in_dhcp_lease is True and str(ip100.mac).lower() == "02:00:5e:00:53:21"
    ip101 = await db_session.get(IPAddress, ips["101"].id)
    await db_session.refresh(ip101)
    assert not ip101.in_dhcp_lease, "過期的租約不算"

    scopes = {s.name: s for s in (await db_session.execute(select(TechnitiumDhcpScope).where(
        TechnitiumDhcpScope.server_id == inst.id))).scalars().all()}
    assert set(scopes) == {"lan", "Default"}
    lan = scopes["lan"]
    assert lan.enabled is True and str(lan.router) == "172.31.250.1"
    assert [str(x) for x in lan.dns_servers] == ["172.31.250.2"] and lan.reservations == 1
    assert lan.exclusions == [{"start": "172.31.250.110", "end": "172.31.250.119"}]
    assert scopes["Default"].enabled is False

    await db_session.refresh(inst)
    assert inst.last_error is None and inst.last_sync_at is not None
    assert inst.last_summary["version"] == "15.6" and inst.last_summary["scopes"] == 2


async def test_scope_removed_on_the_server_is_removed_here(db_session, monkeypatch) -> None:
    await _net(db_session)
    _fake(monkeypatch)
    inst = await _inst(db_session)
    await td.sync_instance(db_session, inst)
    await db_session.commit()
    monkeypatch.setitem(SCOPES, "scopes", [s for s in SCOPES["scopes"] if s["name"] == "lan"])
    try:
        await td.sync_instance(db_session, inst)
        await db_session.commit()
    finally:
        monkeypatch.undo()
    names = (await db_session.execute(select(TechnitiumDhcpScope.name).where(
        TechnitiumDhcpScope.server_id == inst.id))).scalars().all()
    assert names == ["lan"]


async def test_no_permission_is_a_failure_and_clears_nothing(db_session, monkeypatch) -> None:
    await _net(db_session)
    _fake(monkeypatch)
    inst = await _inst(db_session)
    await td.sync_instance(db_session, inst)
    await db_session.commit()
    _fake(monkeypatch, denied=True)
    with pytest.raises(tc.TechnitiumError):
        await td.sync_instance(db_session, inst)
    await db_session.refresh(inst)
    assert "denied" in (inst.last_error or "")
    n = len((await db_session.execute(select(DHCPPoolRange).where(DHCPPoolRange.source_id == inst.id))).all())
    assert n == 2, "讀不到就不清"


async def test_leases_off_does_not_touch_lease_flags(db_session, monkeypatch) -> None:
    await _net(db_session)
    calls: list = []
    _fake(monkeypatch, calls=calls)
    inst = await _inst(db_session, sync_leases=False)
    summary = await td.sync_instance(db_session, inst)
    assert "leases" not in summary
    assert "/api/dhcp/leases/list" not in [c[0] for c in calls]
