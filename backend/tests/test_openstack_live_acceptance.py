"""OpenStack 整合的驗收清單，對真的 OpenStack 跑（scripts/openstack-test-target 架的 DevStack）。

交接文件第 10 節逐項對應；沒有測試靶時整支略過。執行：

    JTIPAM_OPENSTACK_TARGET_ENV=<openstack-target.env> JTIPAM_OPENSTACK_TARGET_SEED=<openstack-target-seed.json> \\
    pytest tests/test_openstack_live_acceptance.py

兩個檔案的做法見 scripts/openstack-test-target/README.md「跑 jt-ipam 的驗收測試」（帳密是測試靶自己隨機產生的，不進版本庫）。
"""
from __future__ import annotations

import ipaddress
import json
import os
import uuid
from typing import Any

import pytest
from app.models.openstack import OpenStackInstance, OpenStackPort, OpenStackSubnet
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.virt import VirtualMachine, VMInterface
from app.services import openstack as svc
from sqlalchemy import select

pytestmark = pytest.mark.anyio

ENV_FILE = os.environ.get("JTIPAM_OPENSTACK_TARGET_ENV")
SEED_FILE = os.environ.get("JTIPAM_OPENSTACK_TARGET_SEED")
skip = pytest.mark.skipif(not (ENV_FILE and SEED_FILE), reason="OpenStack test target not configured")


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(ENV_FILE or "", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v.strip().strip('"')
    return out


def _seed() -> dict[str, Any]:
    with open(SEED_FILE or "", encoding="utf-8") as fh:
        return json.load(fh)


async def _instance(db_session, *, user: str, password: str, project: str | None = None,
                    app_cred: tuple[str, str] | None = None, all_projects: bool = True) -> OpenStackInstance:
    e = _env()
    inst = OpenStackInstance(
        id=uuid.uuid4(), name=f"live-{uuid.uuid4().hex[:6]}", auth_url=e["OS_AUTH_URL"],
        auth_method="app_credential" if app_cred else "password",
        username=None if app_cred else user, user_domain="Default",
        project_name=None if app_cred else project, project_domain="Default",
        app_credential_id=app_cred[0] if app_cred else None,
        region=e.get("OS_REGION_NAME") or None, interface=e.get("OS_INTERFACE", "public"),
        all_projects=all_projects, secret_enc=b"x", secret_nonce=b"y")
    inst.secret_enc, inst.secret_nonce = svc.encrypt_secret_for(inst.id, app_cred[1] if app_cred else password)
    db_session.add(inst)
    await db_session.flush()
    return inst


def _nova_servers() -> dict[str, dict[str, Any]]:
    """直接問 Nova（admin、全部專案、自己翻頁），當作比對的標準答案。"""
    import httpx
    e = _env()
    body = {"auth": {"identity": {"methods": ["password"], "password": {"user": {
        "name": e["ADMIN_USER"], "domain": {"name": "Default"}, "password": e["ADMIN_PASSWORD"]}}},
        "scope": {"project": {"name": "admin", "domain": {"name": "Default"}}}}}
    r = httpx.post(f"{e['OS_AUTH_URL'].rstrip('/')}/auth/tokens", json=body, timeout=30)
    r.raise_for_status()
    h = {"X-Auth-Token": r.headers["X-Subject-Token"], "OpenStack-API-Version": "compute 2.79"}
    out: dict[str, dict[str, Any]] = {}
    marker = None
    while True:
        params: dict[str, Any] = {"all_tenants": 1, "limit": 100}
        if marker:
            params["marker"] = marker
        page = httpx.get(f"{e['NOVA_URL'].rstrip('/')}/servers/detail", params=params, headers=h, timeout=30).json()
        items = page.get("servers") or []
        out.update({x["id"]: x for x in items})
        if len(items) < 100:
            return out
        marker = items[-1]["id"]


async def _vms(db_session, inst) -> dict[str, VirtualMachine]:
    rows = (await db_session.execute(select(VirtualMachine).where(
        VirtualMachine.cluster_id == inst.cluster_id))).scalars().all()
    return {r.external_id: r for r in rows}


@skip
async def test_admin_full_inventory(db_session) -> None:
    """全專案盤點：筆數與平台一致、多網卡、雙堆疊、重疊網段不合併、Floating IP、主機、關機、未接裝置的 port。"""
    e, seed = _env(), _seed()
    # IPAM 裡有一段 192.168.100.0/24：兩個專案都用這個 CIDR，所以不可自動對應到任何一邊
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    db_session.add(Subnet(section_id=sec.id, cidr="192.168.100.0/24"))
    db_session.add(Subnet(section_id=sec.id, cidr="172.31.10.0/24"))
    await db_session.flush()

    inst = await _instance(db_session, user=e["ADMIN_USER"], password=e["ADMIN_PASSWORD"], project="admin")
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["complete"] is True, out
    caps = inst.capabilities or {}
    assert caps.get("all_projects") is True, caps
    assert caps.get("host_visible") is True, caps

    vms = await _vms(db_session, inst)
    servers = _nova_servers()           # 平台當下的樣子（種子檔是建立當時的快照，搬移過就不準）
    assert set(vms) == set(servers), "VM 筆數要跟平台一致（分頁完整）"
    for sid, s in servers.items():
        assert vms[sid].node == s["OS-EXT-SRV-ATTR:hypervisor_hostname"], (s["name"], vms[sid].node)
    by_name = {v.name: v for v in vms.values()}
    assert by_name["a3"].status == "stopped"
    ifaces = (await db_session.execute(select(VMInterface).where(VMInterface.vm_id == by_name["a2"].id))).scalars().all()
    assert len(ifaces) == 2, "a2 有兩張網卡"

    ports = {p.port_id: p for p in (await db_session.execute(select(OpenStackPort).where(
        OpenStackPort.instance_id == inst.id))).scalars().all()}
    # 重疊網段：a1 與 b1 都是 192.168.100.10，但在不同子網路、不同 VM
    tens = [p for p in ports.values() if any(f.get("ip_address") == "192.168.100.10" for f in (p.fixed_ips or []))
            and p.vm_id is not None]
    assert len(tens) == 2
    assert len({p.vm_id for p in tens}) == 2
    assert len({f["subnet_id"] for p in tens for f in p.fixed_ips}) == 2
    # 雙堆疊：c1 同時有 IPv4 與 IPv6
    c1 = [p for p in ports.values() if p.vm_id == by_name["c1"].id]
    vers = {ipaddress.ip_address(f["ip_address"]).version for p in c1 for f in (p.fixed_ips or [])}
    assert vers == {4, 6}
    # Floating IP：綁定的掛在 port 上，未綁定的另外記，不當成 VM 的位址
    fl = {f["floating_ip"]: f for f in seed["floating_ips"]}
    bound = [f for f in fl.values() if f["port_id"]]
    for f in bound:
        p = ports[f["port_id"]]
        assert any(x.get("floating_ip_address") == f["floating_ip"] for x in (p.floating_ips or []))
    unbound = [f["floating_ip"] for f in fl.values() if not f["port_id"]]
    assert set(unbound) <= {x.get("floating_ip_address") for x in caps.get("unbound_floating_ips", [])}
    all_vm_ips = {str(i.primary_ip).split("/")[0] for v in vms.values() for i in (await db_session.execute(
        select(VMInterface).where(VMInterface.vm_id == v.id))).scalars().all() if i.primary_ip}
    assert not (set(fl) & all_vm_ips), "Floating IP 不是客體內部網卡的位址"
    # 沒接裝置的 port 照樣保留，不掛到任何 VM
    assert any(p.vm_id is None and not p.device_id for p in ports.values())

    subs = (await db_session.execute(select(OpenStackSubnet).where(
        OpenStackSubnet.instance_id == inst.id))).scalars().all()
    overlap = [s for s in subs if str(s.cidr) == "192.168.100.0/24"]
    assert len(overlap) == 2
    assert all(s.mapped_subnet_id is None for s in overlap), "重疊網段一律待對應"
    c4 = next(s for s in subs if str(s.cidr) == "172.31.10.0/24")
    assert c4.mapped_subnet_id is not None, "唯一的 CIDR 自動對應"

    # 第二輪：完整同步不會誤刪任何東西
    out2 = await svc.sync_instance(db_session, inst)
    assert (out2["removed_vms"], out2["removed_ports"], out2["removed_subnets"]) == (0, 0, 0)


@skip
async def test_reader_falls_back_and_hides_host(db_session) -> None:
    """reader：Nova 拒絕跨專案（403）→ 退回自己的專案並記下能力；主機欄位看不到時一律 null，不猜。"""
    e = _env()
    inst = await _instance(db_session, user=e["READER_USER"], password=e["READER_PASSWORD"], project=e["READER_PROJECT"])
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    caps = inst.capabilities or {}
    assert caps.get("all_projects") is False, caps
    assert caps.get("host_visible") is False, caps
    vms = await _vms(db_session, inst)
    assert vms, out
    assert all(v.node is None for v in vms.values())
    assert out["removed_vms"] == 0


@skip
async def test_application_credential(db_session) -> None:
    e = _env()
    inst = await _instance(db_session, user="", password="", app_cred=(e["APPCRED_ID"], e["APPCRED_SECRET"]))
    res = await svc.diagnose(inst)
    assert res["ok"] is True, res
    assert res["capabilities"]["services"]["compute"]["ok"] is True


@skip
async def test_wrong_password_is_an_auth_error(db_session) -> None:
    e = _env()
    inst = await _instance(db_session, user=e["READER_USER"], password="definitely-wrong", project=e["READER_PROJECT"])
    res = await svc.diagnose(inst)
    assert res["ok"] is False
    assert (res.get("error") or {}).get("code") == "openstack_auth_failed", res


@skip
async def test_migrate_and_rename_keep_one_vm(db_session) -> None:
    """VM 搬到另一台主機、改名：同步後還是同一筆（以 Nova UUID 識別），主機與名稱跟著換。
    這一項會真的動測試靶（admin 帳號做即時遷移與改名，做完改回原名）。"""
    import asyncio

    import httpx
    e = _env()
    body = {"auth": {"identity": {"methods": ["password"], "password": {"user": {
        "name": e["ADMIN_USER"], "domain": {"name": "Default"}, "password": e["ADMIN_PASSWORD"]}}},
        "scope": {"project": {"name": "admin", "domain": {"name": "Default"}}}}}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{e['OS_AUTH_URL'].rstrip('/')}/auth/tokens", json=body)
        r.raise_for_status()
        h = {"X-Auth-Token": r.headers["X-Subject-Token"], "OpenStack-API-Version": "compute 2.79"}
        nova = e["NOVA_URL"].rstrip("/")
        # fake 驅動在測試靶重啟後，VM 的電源狀態會歸零（Nova 不准遷移）：先硬重開一次讓它回到 running（1）
        srv = next(s for s in (await c.get(f"{nova}/servers/detail", params={"all_tenants": 1, "name": "^bulk-0"},
                                            headers=h)).json()["servers"] if s["status"] == "ACTIVE")
        sid = srv["id"]
        if srv.get("OS-EXT-STS:power_state") != 1:
            await c.post(f"{nova}/servers/{sid}/action", headers=h, json={"reboot": {"type": "HARD"}})
            for _ in range(30):
                await asyncio.sleep(2)
                srv = (await c.get(f"{nova}/servers/{sid}", headers=h)).json()["server"]
                if srv.get("OS-EXT-STS:power_state") == 1 and srv["status"] == "ACTIVE":
                    break
        old_host, old_name = srv["OS-EXT-SRV-ATTR:host"], srv["name"]

        inst = await _instance(db_session, user=e["ADMIN_USER"], password=e["ADMIN_PASSWORD"], project="admin")
        await svc.sync_instance(db_session, inst)
        await db_session.flush()
        before = (await _vms(db_session, inst))[sid]
        assert before.node == old_host

        target = next(x["hypervisor_hostname"] for x in (await c.get(f"{nova}/os-hypervisors/detail", headers=h)
                                                          ).json()["hypervisors"] if x["hypervisor_hostname"] != old_host)
        r = await c.post(f"{nova}/servers/{sid}/action", headers=h,
                         json={"os-migrateLive": {"host": target, "block_migration": "auto"}})
        assert r.status_code == 202, r.text
        for _ in range(60):
            await asyncio.sleep(2)
            cur = (await c.get(f"{nova}/servers/{sid}", headers=h)).json()["server"]
            if cur["OS-EXT-SRV-ATTR:host"] == target and cur["status"] == "ACTIVE":
                break
        else:
            pytest.fail("live migration did not finish")
        await c.put(f"{nova}/servers/{sid}", headers=h, json={"server": {"name": f"{old_name}-renamed"}})
        try:
            await svc.sync_instance(db_session, inst)
            await db_session.flush()
            vms = await _vms(db_session, inst)
            after = vms[sid]
            assert after.id == before.id, "搬移、改名後仍是同一筆 VM"
            assert after.node == target
            assert after.name == f"{old_name}-renamed"
            assert sum(1 for v in vms.values() if v.name.startswith(old_name)) == 1
        finally:
            # 改回原名、搬回原主機：其他驗收項目拿種子資料比對主機
            await c.put(f"{nova}/servers/{sid}", headers=h, json={"server": {"name": old_name}})
            await c.post(f"{nova}/servers/{sid}/action", headers=h,
                         json={"os-migrateLive": {"host": old_host, "block_migration": "auto"}})
            for _ in range(60):
                await asyncio.sleep(2)
                cur = (await c.get(f"{nova}/servers/{sid}", headers=h)).json()["server"]
                if cur["OS-EXT-SRV-ATTR:host"] == old_host and cur["status"] == "ACTIVE":
                    break
