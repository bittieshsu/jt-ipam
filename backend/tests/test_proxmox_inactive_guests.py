"""Proxmox 的範本與關機的 VM（使用者 2026-10-13：「停用的不要抓」）。

以前不管開機與否，都拿設定裡的 cloud-init 靜態 IP、LXC 的 ip=、或網卡 MAC 去對 IP：
報主機名稱、投 MAC 的票（可能把已經被別台重用的 IP 的 MAC 翻掉）、自動建 IP、設主要 IP。
- 範本：不是一台在跑的機器 → 不跟任何 IP 關聯、不報名稱，主要 IP 清掉
- 關機：設定裡的 IP 可能已經給別台用了 → 不報新名稱、不投 MAC 票、不自動建 IP；
  它以前報過的名稱先保留（hold：重開機、暫時關機時名稱不會一消一長），主要 IP 維持原值
"""
from __future__ import annotations

import copy

from sqlalchemy import func, select

from app.models.address import IPAddress
from app.models.ip_hostname import IPHostnameObservation
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.virt import ProxmoxInstance, VirtualMachine
from app.services import proxmox as px

BASE = {
    "/api2/json/version": {"data": {"version": "8.1"}},
    "/api2/json/cluster/status": {"data": [{"type": "node", "name": "pve1", "ip": "198.51.100.250"}]},
    "/api2/json/nodes": {"data": [{"node": "pve1"}]},
    "/api2/json/nodes/pve1/network": {"data": []},
    "/api2/json/nodes/pve1/lxc": {"data": []},
}


def _resp(status: str, *, template: int = 0, ip: str = "198.51.100.30", mac: str = "02:00:00:00:00:30") -> dict:
    r = copy.deepcopy(BASE)
    vm = {"vmid": 100, "name": "web-vm", "status": status}
    if template:
        vm["template"] = template
    r["/api2/json/nodes/pve1/qemu"] = {"data": [vm]}
    r["/api2/json/nodes/pve1/qemu/100/config"] = {"data": {
        "net0": f"virtio={mac},bridge=vmbr0", "ipconfig0": f"ip={ip}/24,gw=198.51.100.1"}}
    return r


def _patch(monkeypatch, resp: dict) -> None:
    async def _fake(_session, _instance, path, *, base_url=None, timeout=None):
        return resp.get(path, {"data": []})
    monkeypatch.setattr(px, "_api_get", _fake)


async def _setup(db, *, auto_create: bool = False):  # type: ignore[no-untyped-def]
    sec = Section(name="px-inactive")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    inst = ProxmoxInstance(api_url="https://pve.example.com:8006", auth_username="root@pam",
                           auth_token_id="jtipam", auto_create_ips=auto_create)
    db.add(inst)
    await db.flush()
    return inst, sub


async def _names(db, ip_id) -> set[str]:  # type: ignore[no-untyped-def]
    return set((await db.execute(select(IPHostnameObservation.hostname).where(
        IPHostnameObservation.ip_id == ip_id, IPHostnameObservation.source == "proxmox"))).scalars().all())


async def test_template_gives_nothing(db_session, monkeypatch) -> None:
    inst, _sub = await _setup(db_session, auto_create=True)
    _patch(monkeypatch, _resp("stopped", template=1))
    await px.sync_instance(db_session, inst)
    await db_session.flush()
    n = (await db_session.execute(select(func.count()).select_from(IPAddress).where(
        func.host(IPAddress.ip) == "198.51.100.30"))).scalar_one()
    assert n == 0, "範本設定裡的 IP 不自動建立"
    vm = (await db_session.execute(select(VirtualMachine))).scalar_one()
    assert vm.is_template is True
    assert vm.primary_ip_id is None


async def test_running_vm_still_links_and_creates(db_session, monkeypatch) -> None:
    inst, _sub = await _setup(db_session, auto_create=True)
    _patch(monkeypatch, _resp("running"))
    await px.sync_instance(db_session, inst)
    await db_session.flush()
    ipa = (await db_session.execute(select(IPAddress).where(
        func.host(IPAddress.ip) == "198.51.100.30"))).scalar_one()
    assert await _names(db_session, ipa.id) == {"web-vm"}
    vm = (await db_session.execute(select(VirtualMachine))).scalar_one()
    assert vm.primary_ip_id == ipa.id


async def test_stopped_vm_keeps_its_name_but_claims_nothing_new(db_session, monkeypatch) -> None:
    inst, sub = await _setup(db_session, auto_create=True)
    # 開機時：命名了 .30、成為主要 IP
    _patch(monkeypatch, _resp("running"))
    await px.sync_instance(db_session, inst)
    await db_session.flush()
    ipa = (await db_session.execute(select(IPAddress).where(
        func.host(IPAddress.ip) == "198.51.100.30"))).scalar_one()
    vm = (await db_session.execute(select(VirtualMachine))).scalar_one()
    assert vm.primary_ip_id == ipa.id

    # 關機、而且設定改成另一個 IP、另一個 MAC：.30 已經給別台（MAC 換了）
    ipa.mac = "02:00:00:00:00:99"
    await db_session.flush()
    _patch(monkeypatch, _resp("stopped", ip="198.51.100.31", mac="02:00:00:00:00:31"))
    await px.sync_instance(db_session, inst)
    await db_session.flush()
    await db_session.refresh(ipa)
    assert str(ipa.mac) == "02:00:00:00:00:99", "關機的 VM 不投 MAC 票"
    assert await _names(db_session, ipa.id) == {"web-vm"}, "以前報過的名稱先保留"
    n = (await db_session.execute(select(func.count()).select_from(IPAddress).where(
        func.host(IPAddress.ip) == "198.51.100.31"))).scalar_one()
    assert n == 0, "關機的 VM 設定裡的新 IP 不自動建立"
    assert vm.primary_ip_id == ipa.id, "主要 IP 維持原值"
