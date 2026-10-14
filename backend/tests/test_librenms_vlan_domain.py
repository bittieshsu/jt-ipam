"""LibreNMS 同步 VLAN 時可以指定寫進哪個 domain（GitHub issue #53）。

回報：從 phpIPAM 搬來的 VLAN 在 default domain（有名稱、子網路接在這裡），LibreNMS 同步卻固定寫進
另一個叫「LibreNMS」的 domain，同一個 VLAN 編號出現兩份，子網路與「哪台交換器帶了這個 VLAN」連不起來。
- 不指定：維持原本的行為（專用的 LibreNMS domain）
- 指定 domain：沿用同編號的既有 VLAN，只補 device↔VLAN 對應；已有的名稱不用交換器上的（VLAN0010）覆寫
- vlan_create_missing 關掉：domain 裡沒有的編號不新建（交換器預設的 1002–1005 不會被帶進來）
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.librenms import LibreNMSDevice, LibreNMSInstance
from app.models.vlan import VLAN, DeviceVLAN, VLANDomain
from app.services import librenms as lib


def _api(monkeypatch) -> None:
    async def fake(_inst, path, *, timeout=30.0):
        if path.endswith("/vlans"):
            return {"vlans": [{"vlan_vlan": 10, "vlan_name": "VLAN0010"},
                              {"vlan_vlan": 1002, "vlan_name": "fddi-default"}]}
        return {}
    monkeypatch.setattr(lib, "_api_get", fake)


async def _setup(db, **kw):  # type: ignore[no-untyped-def]
    inst = LibreNMSInstance(name=f"lnms-{uuid.uuid4().hex[:6]}", api_url="https://librenms.example",
                            api_token_enc=b"x", api_token_nonce=b"y", **kw)
    db.add(inst)
    await db.flush()
    dev = LibreNMSDevice(instance_id=inst.id, legacy_device_id=7, hostname="sw1", status="up")
    db.add(dev)
    await db.flush()
    return inst, dev


async def _default_domain(db):  # type: ignore[no-untyped-def]
    dom = VLANDomain(name=f"default-{uuid.uuid4().hex[:4]}")
    db.add(dom)
    await db.flush()
    server = VLAN(domain_id=dom.id, number=10, name="Server")
    db.add(server)
    await db.flush()
    return dom, server


async def test_without_a_domain_it_still_uses_the_librenms_domain(db_session, monkeypatch) -> None:
    inst, _dev = await _setup(db_session)
    _api(monkeypatch)
    await lib.sync_vlans(db_session, inst)
    await db_session.flush()
    dom = (await db_session.execute(select(VLANDomain).where(VLANDomain.name == lib.LIBRENMS_VLAN_DOMAIN))).scalar_one()
    numbers = set((await db_session.execute(select(VLAN.number).where(VLAN.domain_id == dom.id))).scalars().all())
    assert numbers == {10, 1002}


async def test_chosen_domain_reuses_vlans_and_keeps_their_names(db_session, monkeypatch) -> None:
    dom, server = await _default_domain(db_session)
    inst, dev = await _setup(db_session, vlan_domain_id=dom.id)
    _api(monkeypatch)
    await lib.sync_vlans(db_session, inst)
    await db_session.flush()
    await db_session.refresh(server)
    assert server.name == "Server", "IPAM 取好的名稱不用交換器上的 VLAN0010 覆寫"
    mapped = set((await db_session.execute(select(DeviceVLAN.vlan_id).where(
        DeviceVLAN.librenms_device_id == dev.id))).scalars().all())
    assert server.id in mapped, "對應補在既有那一筆上（子網路接的就是它）"
    tens = (await db_session.execute(select(func.count()).select_from(VLAN).where(VLAN.number == 10))).scalar_one()
    assert tens == 1, "同一個編號不再出現兩份"
    assert (await db_session.execute(select(func.count()).select_from(VLANDomain).where(
        VLANDomain.name == lib.LIBRENMS_VLAN_DOMAIN))).scalar_one() == 0, "不再另建 LibreNMS domain"
    created = (await db_session.execute(select(VLAN).where(VLAN.domain_id == dom.id, VLAN.number == 1002))).scalar_one()
    assert created.name == "fddi-default", "domain 裡沒有的照樣新建（預設）"


async def test_only_existing_vlans_when_create_missing_is_off(db_session, monkeypatch) -> None:
    dom, server = await _default_domain(db_session)
    inst, dev = await _setup(db_session, vlan_domain_id=dom.id, vlan_create_missing=False)
    _api(monkeypatch)
    await lib.sync_vlans(db_session, inst)
    await db_session.flush()
    numbers = set((await db_session.execute(select(VLAN.number).where(VLAN.domain_id == dom.id))).scalars().all())
    assert numbers == {10}, "交換器預設的 1002 不會被帶進來"
    mapped = set((await db_session.execute(select(DeviceVLAN.vlan_id).where(
        DeviceVLAN.librenms_device_id == dev.id))).scalars().all())
    assert mapped == {server.id}


async def test_api_sets_and_clears_the_domain(client, auth_headers, db_session) -> None:
    dom, _server = await _default_domain(db_session)
    await db_session.commit()
    r = await client.post("/api/v1/librenms/instances", headers=auth_headers, json={
        "name": f"lnms-{uuid.uuid4().hex[:6]}", "api_url": "https://librenms.example", "api_token": "x" * 16,
        "vlan_domain_id": str(dom.id), "vlan_create_missing": False})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["vlan_domain_id"] == str(dom.id)
    assert body["vlan_create_missing"] is False
    iid = body["id"]
    r = await client.patch(f"/api/v1/librenms/instances/{iid}", headers=auth_headers, json={"enabled": True})
    assert r.json()["vlan_domain_id"] == str(dom.id), "沒送這個欄位就不動"
    r = await client.patch(f"/api/v1/librenms/instances/{iid}", headers=auth_headers, json={"vlan_domain_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["vlan_domain_id"] is None, "送 null＝改回專用的 LibreNMS domain"
    r = await client.patch(f"/api/v1/librenms/instances/{iid}", headers=auth_headers,
                           json={"vlan_domain_id": str(uuid.uuid4())})
    assert r.status_code == 422, r.text
