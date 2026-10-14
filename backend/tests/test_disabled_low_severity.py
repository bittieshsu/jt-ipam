"""停用的東西不採用：DHCP 固定分配、別名、位址清單、租約、虛擬機（使用者 2026-10-13：「停用的不要抓」）。

盤點時列為低嚴重度的幾項：大多是「發放範圍早就跳過停用的，但同一個來源的固定分配／租約沒有」。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.models.address import IPAddress
from app.models.dhcp import DHCPReservation
from app.models.section import Section
from app.models.subnet import Subnet


def test_opnsense_isc_static_maps_of_a_disabled_interface_are_ignored() -> None:
    from app.services.opnsense_firewall import _parse_legacy_static_maps
    xml = ("<opnsense><dhcpd>"
           "<lan><enable>1</enable><staticmap><mac>02:00:00:00:00:01</mac><ipaddr>198.51.100.10</ipaddr></staticmap></lan>"
           "<opt1><staticmap><mac>02:00:00:00:00:02</mac><ipaddr>198.51.100.20</ipaddr></staticmap></opt1>"
           "<opt2><enable>0</enable><staticmap><mac>02:00:00:00:00:03</mac><ipaddr>198.51.100.30</ipaddr></staticmap></opt2>"
           "<opt3><enable/><staticmap><mac>02:00:00:00:00:04</mac><ipaddr>198.51.100.40</ipaddr></staticmap></opt3>"
           "</dhcpd></opnsense>")
    assert sorted(r.ip for r in _parse_legacy_static_maps(xml)) == ["198.51.100.10", "198.51.100.40"], \
        "跟發放範圍同一條規則：沒有 <enable> 或 enable=0 的介面不算；<enable/> 是啟用"


async def test_disabled_opnsense_alias_is_not_a_membership(db_session) -> None:
    from app.models.firewall import OPNsenseFirewall, OPNsenseSyncedAlias
    from app.services.fw_lookup import rules_touching_ip
    fw = OPNsenseFirewall(name=f"fw-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.254",
                          api_key_enc=b"x", api_key_nonce=b"x", api_secret_enc=b"x", api_secret_nonce=b"x")
    db_session.add(fw)
    await db_session.flush()
    db_session.add_all([
        OPNsenseSyncedAlias(firewall_id=fw.id, name="on_hosts", enabled=True, content=["198.51.100.7"]),
        OPNsenseSyncedAlias(firewall_id=fw.id, name="off_hosts", enabled=False, content=["198.51.100.7"]),
    ])
    await db_session.flush()
    out = await rules_touching_ip(db_session, "198.51.100.7")
    names = {a["name"] for a in out["aliases"]}
    assert names == {"on_hosts"}


async def test_fortigate_reservations_of_a_disabled_server_are_ignored(db_session, monkeypatch) -> None:
    from app.models.fortigate import FortiGateFirewall
    from app.services import fortigate as fg
    fw = FortiGateFirewall(name=f"fg-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.1",
                           api_token_enc=b"x", api_token_nonce=b"x")
    db_session.add(fw)
    await db_session.flush()

    async def _all(_fw, _path, vdoms):
        return {"root": [
            {"status": "enable", "reserved-address": [{"ip": "198.51.100.10", "mac": "02:00:00:00:00:10"}]},
            {"status": "disable", "reserved-address": [{"ip": "198.51.100.20", "mac": "02:00:00:00:00:20"}]},
        ]}
    monkeypatch.setattr(fg, "_fetch_all_vdoms", _all)
    await fg.sync_dhcp_reservations(db_session, fw, ["root"])
    await db_session.flush()
    ips = set((await db_session.execute(select(DHCPReservation.ip).where(
        DHCPReservation.source_id == fw.id))).scalars().all())
    assert ips == {"198.51.100.10"}


async def test_pfsense_static_maps_under_a_disabled_interface_are_ignored(db_session, monkeypatch) -> None:
    from app.models.pfsense import PfSenseFirewall
    from app.services import pfsense as pf
    fw = PfSenseFirewall(name=f"pf-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.2",
                         api_key_enc=b"x", api_key_nonce=b"x")
    db_session.add(fw)
    await db_session.flush()

    async def _get(_fw, path, *, timeout=None):
        if path == pf.EP_DHCP_SERVERS:
            return [{"id": "lan", "enable": True}, {"id": "opt1", "enable": False}]
        return [{"parent_id": "lan", "ipaddr": "198.51.100.10", "mac": "02:00:00:00:00:10"},
                {"parent_id": "opt1", "ipaddr": "198.51.100.20", "mac": "02:00:00:00:00:20"},
                {"ipaddr": "198.51.100.30", "mac": "02:00:00:00:00:30"}]
    monkeypatch.setattr(pf, "_api_get", _get)
    await pf.sync_dhcp_reservations(db_session, fw)
    await db_session.flush()
    ips = set((await db_session.execute(select(DHCPReservation.ip).where(
        DHCPReservation.source_id == fw.id))).scalars().all())
    assert ips == {"198.51.100.10", "198.51.100.30"}, "沒有 parent_id 的照收（欄位沒逐版驗證）"


async def test_technitium_leases_of_a_disabled_scope_are_ignored(db_session, monkeypatch) -> None:
    from app.services import technitium_dhcp as td

    from tests import test_technitium_dhcp as base
    ips = await base._net(db_session)
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    db_session.add(sub)
    await db_session.flush()
    off_ip = IPAddress(subnet_id=sub.id, ip="203.0.113.10", state="active")
    db_session.add(off_ip)
    await db_session.commit()
    real = base._leases

    def _leases():  # 停用範圍「Default」裡還有一筆沒到期的租約
        d = real()
        d["leases"].append({"scope": "Default", "type": "Dynamic", "hardwareAddress": "02-00-5E-00-53-99",
                            "address": "203.0.113.10", "hostName": "ghost", "leaseObtained": base._iso(-timedelta(minutes=5)),
                            "leaseExpires": base._iso(timedelta(minutes=55))})
        return d
    monkeypatch.setattr(base, "_leases", _leases)
    base._fake(monkeypatch)
    inst = await base._inst(db_session)
    await td.sync_instance(db_session, inst)
    await db_session.commit()
    await db_session.refresh(off_ip)
    assert not off_ip.in_dhcp_lease, "停用範圍的租約不算"
    ip100 = await db_session.get(IPAddress, ips["100"].id)
    await db_session.refresh(ip100)
    assert ip100.in_dhcp_lease is True


async def test_esxi_powered_off_or_template_vm_creates_no_ip(db_session, monkeypatch) -> None:
    from app.services import esxi

    from tests import test_esxi_sync as base
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    db_session.add(Subnet(section_id=sec.id, cidr="198.51.100.0/24"))
    await db_session.flush()
    inst = await base._inst(db_session)
    inst.auto_create_ips = True
    vms = [
        {"moid": "vm-1", "name": "on", "power_state": "poweredOn", "is_template": False,
         "ip": "198.51.100.11", "nics": []},
        {"moid": "vm-2", "name": "off", "power_state": "poweredOff", "is_template": False,
         "ip": "198.51.100.12", "nics": []},
        {"moid": "vm-3", "name": "tpl", "power_state": "poweredOff", "is_template": True,
         "ip": "198.51.100.13", "nics": []},
    ]
    base._patch(monkeypatch, vms)
    await esxi.sync_instance(db_session, inst)
    await db_session.flush()
    made = set((await db_session.execute(select(func.host(IPAddress.ip)))).scalars().all())
    assert "198.51.100.11" in made
    assert "198.51.100.12" not in made and "198.51.100.13" not in made
