"""虛實標示：IP／MAC 對得到 VM 網卡才標「虛擬機」；對不到**不得**斷言實體機。

虛擬化整合可能只涵蓋部分叢集 —— 「查無」的意思是「不知道」，不是「實體機」。
把「不知道」顯示成「實體機」是誤導，比不顯示更糟。
"""
from __future__ import annotations

import uuid

import pytest

from app.services.fw_lookup import vm_match_for


async def _mk_vm(db_session, *, name: str, ip: str | None, mac: str | None):
    from app.models.virt import VirtCluster, VirtualMachine, VMInterface
    cl = VirtCluster(name=f"cl-{uuid.uuid4().hex[:6]}", type="proxmox")
    db_session.add(cl)
    await db_session.flush()
    vm = VirtualMachine(cluster_id=cl.id, name=name)
    db_session.add(vm)
    await db_session.flush()
    db_session.add(VMInterface(vm_id=vm.id, name="net0", primary_ip=ip, mac=mac))
    await db_session.flush()
    return cl, vm


@pytest.mark.anyio
async def test_matches_by_ip(db_session) -> None:
    await _mk_vm(db_session, name="web-vm", ip="198.51.100.30", mac=None)
    m = await vm_match_for(db_session, ip="198.51.100.30")
    assert m and m["vm"] == "web-vm" and m["platform"] == "proxmox"


@pytest.mark.anyio
async def test_matches_by_mac_when_ip_differs(db_session) -> None:
    """VM 換了 IP（或 PVE 沒回報 IP）→ MAC 仍對得到。"""
    await _mk_vm(db_session, name="db-vm", ip=None, mac="00:00:5e:00:53:44")
    m = await vm_match_for(db_session, ip="203.0.113.99", macs=["00:00:5e:00:53:44"])
    assert m and m["vm"] == "db-vm"


@pytest.mark.anyio
async def test_no_match_returns_none_not_physical(db_session) -> None:
    """對不到 → None。呼叫端據此「不顯示」，不是顯示「實體機」。"""
    assert await vm_match_for(db_session, ip="203.0.113.1") is None
    assert await vm_match_for(db_session) is None, "沒給任何條件不可以亂配"


@pytest.mark.anyio
async def test_ip_read_carries_virt_vm(client, auth_headers, db_session) -> None:
    from app.models.address import IPAddress
    from app.models.section import Section
    from app.models.subnet import Subnet

    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    ipa = IPAddress(subnet_id=sub.id, ip="198.51.100.31", state="used")
    db_session.add(ipa)
    await _mk_vm(db_session, name="app-vm", ip="198.51.100.31", mac=None)
    await db_session.commit()

    r = await client.get(f"/api/v1/addresses/{ipa.id}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["virt_vm"]["vm"] == "app-vm"
