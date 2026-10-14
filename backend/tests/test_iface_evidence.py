"""整合回報的設備介面位址＝上線證據 `iface:<整合>`（客戶 2026-10-14）。

客戶：同一台 gateway 接多個 LAN，每個 LAN 的閘道（路由器自己的介面位址）不回 ping 但一直在用，登記後是紅燈。
整合本來就知道這些位址設在它哪個介面上：API 這一輪有回應、介面是啟用的，就在對得到的 IP 記錄蓋上時間。
設備掛了同步就失敗、不再蓋時間，證據自然過期 → 一起變紅（不可以永遠綠）。

格式來源：OPNsense／pfSense／LibreNMS 是正式站台實機回應（2026-10-14 唯讀探測）；FortiGate、MikroTik 照官方 API 文件。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.address import IPAddress
from app.models.section import Section
from app.models.subnet import Subnet
from app.services import iface_evidence as ie

pytestmark = pytest.mark.anyio

OPN = [
    {"identifier": "lan", "description": "LAN", "status": "up", "enabled": True, "addr4": "192.0.2.1/24",
     "ipv4": [{"ipaddr": "192.0.2.1/24"}], "is_physical": True},
    {"identifier": "opt3", "description": "VLAN20", "status": "up", "enabled": True, "addr4": "198.51.100.1/24",
     "ipv4": [{"ipaddr": "198.51.100.1/24"}, {"ipaddr": "198.51.100.2/24"}]},
    {"identifier": "lo0", "description": "Loopback", "status": "up", "enabled": True, "ipv4": [{"ipaddr": "127.0.0.1/8"}]},
    {"identifier": "opt1", "description": "wgopnsiteb", "status": "up", "enabled": False,
     "ipv4": [{"ipaddr": "203.0.113.2/24"}]},                                     # 設定上沒啟用
    {"identifier": "opt4", "description": "VLAN30", "status": "down", "enabled": True,
     "ipv4": [{"ipaddr": "203.0.113.9/24"}]},                                     # 介面 down
]


def test_opnsense_addresses() -> None:
    assert ie.opnsense_addrs(OPN) == ["192.0.2.1", "198.51.100.1", "198.51.100.2"]


def test_pfsense_addresses() -> None:
    data = {"data": [
        {"name": "wan", "descr": "WAN", "status": "up", "enable": False, "ipaddr": "192.0.2.11", "subnet": "255.255.255.0"},
        {"name": "opt1", "descr": "LAN2", "status": "up", "ipaddr": "198.51.100.1", "ipaddrv6": "2001:db8::1"},
        {"name": "opt2", "descr": "OFF", "status": "down", "ipaddr": "203.0.113.1"},
        {"name": "opt3", "descr": "DHCP", "status": "up", "ipaddr": "dhcp"},
    ]}
    # pfSense 的 enable 欄位不可靠（實機 WAN 在用卻回 false）：只看 status
    assert ie.pfsense_addrs(data) == ["192.0.2.11", "198.51.100.1", "2001:db8::1"]


def test_fortigate_addresses() -> None:
    results = [
        {"name": "port1", "vdom": "root", "ip": "192.0.2.99 255.255.255.0", "status": "up",
         "secondaryip": [{"id": 1, "ip": "192.0.2.98 255.255.255.0"}]},
        {"name": "vlan20", "vdom": "root", "ip": "198.51.100.1 255.255.255.0", "status": "up", "type": "vlan"},
        {"name": "port9", "vdom": "root", "ip": "0.0.0.0 0.0.0.0", "status": "up"},
        {"name": "port8", "vdom": "root", "ip": "203.0.113.1 255.255.255.0", "status": "down"},
    ]
    assert ie.fortigate_addrs(results) == ["192.0.2.99", "192.0.2.98", "198.51.100.1"]


def test_mikrotik_addresses() -> None:
    rows = [
        {".id": "*1", "address": "192.0.2.1/24", "interface": "bridge", "disabled": "false", "invalid": "false"},
        {".id": "*2", "address": "198.51.100.1/24", "interface": "vlan20", "disabled": "false", "invalid": "false",
         "dynamic": "true"},
        {".id": "*3", "address": "203.0.113.1/24", "interface": "ether5", "disabled": "true", "invalid": "false"},
        {".id": "*4", "address": "203.0.113.2/24", "interface": "ether6", "disabled": "false", "invalid": "true"},
    ]
    assert ie.mikrotik_addrs(rows) == ["192.0.2.1", "198.51.100.1"]


def test_librenms_addresses() -> None:
    addrs = [{"ipv4_address": "192.0.2.1", "port_id": 48}, {"ipv4_address": "198.51.100.1", "port_id": 49},
             {"ipv4_address": "203.0.113.1", "port_id": 50}, {"ipv4_address": "203.0.113.2", "port_id": 51},
             {"ipv4_address": "203.0.113.3", "port_id": 999}]
    ports = [{"port_id": 48, "device_id": 7, "ifOperStatus": "up"}, {"port_id": 49, "device_id": 7, "ifOperStatus": "up"},
             {"port_id": 50, "device_id": 7, "ifOperStatus": "down"},                  # 埠 down
             {"port_id": 51, "device_id": 8, "ifOperStatus": "up"}]                    # 設備 8 不在線
    assert ie.librenms_addrs(addrs, ports, up_devices={7}) == ["192.0.2.1", "198.51.100.1"]


async def test_stamp_marks_existing_records_only(db_session) -> None:
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    gw = IPAddress(subnet_id=sub.id, ip="198.51.100.1", state="used")
    db_session.add(gw)
    await db_session.flush()
    out = await ie.stamp(db_session, "iface:opnsense", ["198.51.100.1", "198.51.100.77"], scope_ids=None)
    assert out == {"seen": 2, "matched": 1}
    assert "iface:opnsense" in (gw.arp_seen or {})
    from sqlalchemy import func, select
    n = (await db_session.execute(select(func.count()).select_from(IPAddress).where(
        IPAddress.subnet_id == sub.id))).scalar_one()
    assert n == 1, "介面位址不自動建 IP 記錄"


async def _gateway(db):  # type: ignore[no-untyped-def]
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    gw = IPAddress(subnet_id=sub.id, ip="198.51.100.1", state="used")
    db.add(gw)
    await db.flush()
    return gw


B = b"x"


async def test_opnsense_sync_stamps_its_interfaces(db_session, monkeypatch) -> None:
    from app.models.firewall import OPNsenseFirewall
    from app.services import opnsense_firewall as opn
    gw = await _gateway(db_session)
    fw = OPNsenseFirewall(name=f"opn-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.1", api_key_enc=B,
                          api_key_nonce=B, api_secret_enc=B, api_secret_nonce=B)
    db_session.add(fw)
    await db_session.flush()

    async def fake(_fw, path, **_k):  # type: ignore[no-untyped-def]
        assert path == "/api/interfaces/overview/export"
        return OPN
    monkeypatch.setattr(opn, "_api_get", fake)
    out = await opn.sync_interface_ips(db_session, fw)
    assert out["matched"] == 1
    assert "iface:opnsense" in gw.arp_seen


async def test_pfsense_sync_stamps_its_interfaces(db_session, monkeypatch) -> None:
    from app.models.pfsense import PfSenseFirewall
    from app.services import pfsense as pf
    gw = await _gateway(db_session)
    fw = PfSenseFirewall(name=f"pf-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.11", api_key_enc=B, api_key_nonce=B)
    db_session.add(fw)
    await db_session.flush()

    async def fake(_fw, path, **_k):  # type: ignore[no-untyped-def]
        assert path == pf.EP_STATUS_INTERFACES
        return {"data": [{"name": "opt1", "status": "up", "ipaddr": "198.51.100.1"}]}
    monkeypatch.setattr(pf, "_api_get", fake)
    assert await pf.sync_interface_ips(db_session, fw) == 1
    assert "iface:pfsense" in gw.arp_seen


async def test_fortigate_sync_stamps_its_interfaces(db_session, monkeypatch) -> None:
    from app.models.fortigate import FortiGateFirewall
    from app.services import fortigate as fg
    gw = await _gateway(db_session)
    fw = FortiGateFirewall(name=f"fg-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.20", api_token_enc=B,
                           api_token_nonce=B)
    db_session.add(fw)
    await db_session.flush()
    calls: list[str | None] = []

    async def fake(_fw, path, *, vdom=None, **_k):  # type: ignore[no-untyped-def]
        assert path == "/api/v2/cmdb/system/interface"
        calls.append(vdom)
        # 真的 _api_get 已經把外層的 results 拆掉了
        return [{"name": "vlan20", "ip": "198.51.100.1 255.255.255.0", "status": "up"}]
    monkeypatch.setattr(fg, "_api_get", fake)
    assert await fg.sync_interface_ips(db_session, fw, ["root", "dmz"]) == 1
    assert calls == ["root", "dmz"], "每個 VDOM 都要查"
    assert "iface:fortigate" in gw.arp_seen


async def test_mikrotik_sync_stamps_its_addresses(db_session, monkeypatch) -> None:
    from app.models.mikrotik import MikroTikRouter
    from app.services import mikrotik as mt
    gw = await _gateway(db_session)
    r = MikroTikRouter(name=f"mt-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.30", api_username="u",
                       api_password_enc=B, api_password_nonce=B)
    db_session.add(r)
    await db_session.flush()

    async def fake(_r, ep, **_k):  # type: ignore[no-untyped-def]
        assert ep == mt.EP_IP_ADDRESS
        return [{"address": "198.51.100.1/24", "interface": "vlan20", "disabled": "false", "invalid": "false"}]
    monkeypatch.setattr(mt, "_get", fake)
    out = await mt.sync_interface_ips(db_session, r, client=None)  # type: ignore[arg-type]
    assert out == {"interface_ips": 1}
    assert "iface:mikrotik" in gw.arp_seen


async def test_librenms_sync_stamps_interfaces_of_up_devices(db_session, monkeypatch) -> None:
    from app.models.librenms import LibreNMSDevice, LibreNMSInstance
    from app.services import librenms as lib
    gw = await _gateway(db_session)
    inst = LibreNMSInstance(name=f"l-{uuid.uuid4().hex[:6]}", api_url="https://librenms.example",
                            api_token_enc=B, api_token_nonce=B)
    db_session.add(inst)
    await db_session.flush()
    db_session.add_all([LibreNMSDevice(instance_id=inst.id, legacy_device_id=7, hostname="r1", status="up"),
                        LibreNMSDevice(instance_id=inst.id, legacy_device_id=8, hostname="r2", status="down")])
    await db_session.flush()

    async def fake(_i, path, **_k):  # type: ignore[no-untyped-def]
        if path.startswith("/api/v0/resources/ip/addresses"):
            return {"ip_addresses": [{"ipv4_address": "198.51.100.1", "port_id": 48},
                                     {"ipv4_address": "198.51.100.2", "port_id": 60}]}
        assert path.startswith("/api/v0/ports?columns=")
        return {"ports": [{"port_id": 48, "device_id": 7, "ifOperStatus": "up"},
                          {"port_id": 60, "device_id": 8, "ifOperStatus": "up"}]}
    monkeypatch.setattr(lib, "_api_get", fake)
    out = await lib.sync_interface_ips(db_session, inst)
    assert out == {"seen": 1, "matched": 1}
    assert "iface:librenms" in gw.arp_seen
