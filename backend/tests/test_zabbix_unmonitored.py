"""Zabbix 不監控／維護中的主機（使用者 2026-10-13：「停用的不要抓…例如 zabbix」）。

以前 `status`（0＝監控、1＝不監控）與 `maintenance_status` 有抓回來但只存著：不監控的主機照樣
命名它的 IP、關聯 IP、可用性是 up 就算上線。
- 不監控：不報名稱、不當上線證據、不關聯 IP（位址可能早就給別台用了）；鏡像照樣留著，畫面看得到
- 維護中且不收資料（maintenance_type=1）：可用性停在進維護前的值 → 不當上線證據，名稱照用
- 一般維護（照收資料）：照常
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.address import IPAddress
from app.models.ip_hostname import IPHostnameObservation
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.zabbix import ZabbixHost, ZabbixInstance
from app.services import zabbix as zbx


async def _setup(db):  # type: ignore[no-untyped-def]
    sec = Section(name=f"z-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sn = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sn)
    await db.flush()
    ips = {}
    for n in (1, 2, 3, 4):
        ip = IPAddress(subnet_id=sn.id, ip=f"198.51.100.{n}", state="used")
        db.add(ip)
        ips[n] = ip
    inst = ZabbixInstance(name=f"zbx-{uuid.uuid4().hex[:6]}", api_url="https://zbx.example")
    db.add(inst)
    await db.commit()
    return inst, ips


def _host(n: int, *, status: str = "0", maint: str = "0", mtype: str = "0") -> dict:
    return {"hostid": str(100 + n), "host": f"h{n}", "name": f"host-{n}", "status": status,
            "maintenance_status": maint, "maintenance_type": mtype, "tags": [], "inventory": [],
            "hostgroups": [{"name": "Linux"}],
            "interfaces": [{"ip": f"198.51.100.{n}", "dns": "", "useip": "1", "type": "1", "main": "1",
                            "available": "1"}]}


async def test_unmonitored_and_no_data_maintenance(db_session, monkeypatch) -> None:
    inst, ips = await _setup(db_session)
    hosts = [_host(1), _host(2, status="1"), _host(3, maint="1", mtype="1"), _host(4, maint="1", mtype="0")]
    seen_params: dict = {}

    async def fake_rpc(_inst, method, params, *, auth=None, timeout=30.0):
        if method == "host.get":
            seen_params.update(params)
        return "7.0.0" if method == "apiinfo.version" else hosts

    async def fake_token(_inst, *, major=None):
        return "tok"
    monkeypatch.setattr(zbx, "_rpc", fake_rpc)
    monkeypatch.setattr(zbx, "_auth_token", fake_token)
    await zbx.sync_instance(db_session, inst)
    await db_session.flush()
    assert "maintenance_type" in seen_params["output"], "要問 Zabbix 維護時收不收資料"

    for ip in ips.values():
        await db_session.refresh(ip)
    assert ips[1].last_seen_zabbix is not None
    assert ips[2].last_seen_zabbix is None, "不監控的主機不是上線證據"
    assert ips[3].last_seen_zabbix is None, "維護中不收資料：可用性是停住的"
    assert ips[4].last_seen_zabbix is not None, "一般維護照收資料，照常"

    async def names(ip):  # type: ignore[no-untyped-def]
        return (await db_session.execute(select(func.count()).select_from(IPHostnameObservation).where(
            IPHostnameObservation.ip_id == ip.id, IPHostnameObservation.source == "zabbix"))).scalar_one()
    assert await names(ips[1]) == 1
    assert await names(ips[2]) == 0, "不監控的主機不命名 IP"
    assert await names(ips[3]) == 1

    rows = {h.hostid: h for h in (await db_session.execute(
        select(ZabbixHost).where(ZabbixHost.instance_id == inst.id))).scalars()}
    assert set(rows) == {"101", "102", "103", "104"}, "鏡像照樣留著"
    assert rows["102"].status == "unmonitored"
    assert rows["102"].jt_ipam_address_id is None, "不監控的主機不關聯 IP"
    assert rows["101"].jt_ipam_address_id == ips[1].id
