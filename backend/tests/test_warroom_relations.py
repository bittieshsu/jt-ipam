"""戰情室 09 IP 物件關係（規格 SPEC.md 第 6 節）。

守的是：
1. 同一個物件只有一個節點；焦點 IP 是一個節點、多條有名稱的邊
2. 焦點輪播依關係種類多寡、位址、記錄 ID 決定，重新整理順序不變
3. 範圍與權限：重疊的私有網段不串資料（看記錄 ID 與單位，不看位址字串）
4. 關聯待補只算適用的物件（PC 不缺機櫃、VM 沒回報位址不等於沒有 IP）
5. 來源不一致畫成「待確認」，不挑一條
6. 沒有權限是 forbidden、數字是 None，不是 0
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.address import IPAddress
from app.models.customer import Customer
from app.models.device import Device
from app.models.location import Location, Rack
from app.models.permission import Permission
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from app.models.virt import ProxmoxInstance, VirtCluster, VirtualMachine, VMInterface

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_cache():
    from app.api.v1.endpoints import warroom
    warroom._cache.clear()
    yield
    warroom._cache.clear()


def _hdr(user: User) -> dict[str, str]:
    from app.services.auth import issue_access_token
    return {"Authorization": f"Bearer {issue_access_token(user)}"}


async def _user(db) -> User:
    from app.core.security import hash_password
    u = User(username=f"u-{uuid.uuid4().hex[:8]}", email=f"u-{uuid.uuid4().hex[:8]}@test.local",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True, is_admin=False)
    db.add(u)
    await db.commit()
    return u


async def _page(client, headers, page: int = 1) -> dict:
    r = await client.get(f"/api/v1/warroom/pages/relations?page={page}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _u(*names: str) -> str:
    return f"{'-'.join(names)}-{uuid.uuid4().hex[:4]}"


async def _net(db, cidr: str, *, customer: Customer | None = None, section: Section | None = None) -> Subnet:
    if section is None:
        section = Section(name=_u("內部網路"))
        db.add(section)
        await db.flush()
    s = Subnet(section_id=section.id, cidr=cidr, description="內部伺服器區",
               customer_id=customer.id if customer else None)
    db.add(s)
    await db.flush()
    return s


async def _ip(db, subnet: Subnet, ip: str, **kw) -> IPAddress:
    a = IPAddress(subnet_id=subnet.id, ip=ip, **kw)
    db.add(a)
    await db.flush()
    return a


async def _vm_site(db, *, link_device: bool) -> dict:
    """VM 的位址：主機 pve-node-1 裝在台北總部 R1-02，VM mail1 的 net0 回報 10.20.0.10。"""
    loc = Location(name=_u("台北總部"))
    db.add(loc)
    await db.flush()
    rack = Rack(name=_u("R1-02"), location_id=loc.id)
    db.add(rack)
    await db.flush()
    host = Device(name=_u("pve-node-1"), type="server", rack_id=rack.id)
    db.add(host)
    cl = VirtCluster(name=_u("pve-cluster"), type="proxmox")
    db.add(cl)
    await db.flush()
    db.add(ProxmoxInstance(cluster_id=cl.id, api_url="https://pve.example.test:8006", auth_username="root@pam",
                           auth_token_id="t", last_sync_at=datetime.now(UTC) - timedelta(minutes=3)))
    net = await _net(db, "10.20.0.0/24")
    guest = None
    if link_device:
        guest = Device(name=_u("mail1"), type="server")
        db.add(guest)
        await db.flush()
    ip = await _ip(db, net, "10.20.0.10", hostname="mail1.example.test", mac="52:54:00:aa:bb:01",
                   device_id=guest.id if guest else None, last_seen_scanner=datetime.now(UTC) - timedelta(minutes=5))
    vm = VirtualMachine(cluster_id=cl.id, name=_u("mail1"), node=host.name, kind="vm", status="running",
                        legacy_vmid=120, primary_ip_id=ip.id)
    db.add(vm)
    await db.flush()
    nic = VMInterface(vm_id=vm.id, name="net0", mac="52:54:00:aa:bb:01", primary_ip="10.20.0.10")
    db.add(nic)
    await db.commit()
    return {"loc": loc, "rack": rack, "host": host, "cluster": cl, "net": net, "ip": ip, "vm": vm, "nic": nic,
            "guest": guest}


def _keys(focus: dict) -> list[str]:
    return [n["key"] for n in focus["nodes"]]


# ─────────────────────────── 1. 一個物件一個節點 ───────────────────────────

async def test_vm_address_uses_host_placement_and_one_node_per_object(client, auth_headers, db_session) -> None:
    s = await _vm_site(db_session, link_device=False)
    d = await _page(client, auth_headers)
    f = d["focus"]
    assert f["state"] == "ok"
    keys = _keys(f)
    assert len(keys) == len(set(keys))                                   # 同一個物件只有一個節點
    ipk = f"ip:{s['ip'].id}"
    assert keys.count(ipk) == 1
    by = {n["key"]: n for n in f["nodes"]}
    # IP 沒有連到裝置：實體列放 VM 所在的主機，連同它的機櫃與地點
    assert by[f"device:{s['host'].id}"]["row"] == "physical"
    assert by[f"rack:{s['rack'].id}"]["row"] == "physical"
    assert by[f"location:{s['loc'].id}"]["row"] == "physical"
    assert by[f"vm:{s['vm'].id}"]["row"] == "virtual"
    assert by[f"nic:{s['nic'].id}"]["row"] == "virtual"
    assert 6 <= len(keys) <= 9
    # 主機只有一個節點，主機 → VM 的邊從實體列那個節點出發
    edges = {(e["from"], e["to"]): e for e in f["edges"]}
    host_vm = edges[(f"device:{s['host'].id}", f"vm:{s['vm'].id}")]
    assert host_vm["kind"] == "host_vm"
    assert host_vm["source"] == "proxmox"
    assert host_vm["line"] == "dashed"                                  # 主機是靠節點名稱＝裝置名稱推出來的
    # 多條有名稱的邊接到同一個 IP 節點
    into_ip = sorted(e["kind"] for e in f["edges"] if e["to"] == ipk)
    assert into_ip == ["nic_ip", "subnet_ip"]
    nic_ip = edges[(f"nic:{s['nic'].id}", ipk)]
    assert nic_ip["line"] == "solid"
    assert nic_ip["state"] == "ok"
    assert nic_ip["at"]                                                 # 來源最後同步時間
    assert all(e["source"] and e["kind"] for e in f["edges"])
    assert d["metrics"] == {"ips": 1, "devices": 1, "virt": 1}
    assert d["counts"]["locations"] == 1
    assert d["counts"]["racks"] == 1
    assert d["counts"]["subnets"] == 1
    assert d["counts"]["sections"] == 1
    assert [s_["source"] for s_ in f["sources"]][:2] == ["proxmox", "scanner"]
    assert f["last_confirmed"]


async def test_host_matched_by_name_is_derived(client, auth_headers, db_session) -> None:
    s = await _vm_site(db_session, link_device=True)
    d = await _page(client, auth_headers)
    f = d["focus"]
    keys = _keys(f)
    assert len(keys) == len(set(keys))
    by = {n["key"]: n for n in f["nodes"]}
    # IP 連到客體自己的裝置記錄：實體列是那台裝置（沒有機櫃就不畫機櫃），主機放虛擬化列、位置寫成文字
    assert by[f"device:{s['guest'].id}"]["row"] == "physical"
    assert f"rack:{s['rack'].id}" not in by
    host = by[f"device:{s['host'].id}"]
    assert host["row"] == "virtual"
    assert s["rack"].name in host["sub"]
    assert s["loc"].name in host["sub"]
    edges = {(e["from"], e["to"]): e for e in f["edges"]}
    # 主機是靠「節點名稱＝裝置名稱」推出來的：虛線
    assert edges[(f"device:{s['host'].id}", f"vm:{s['vm'].id}")]["line"] == "dashed"
    into_ip = sorted(e["kind"] for e in f["edges"] if e["to"] == f"ip:{s['ip'].id}")
    assert into_ip == ["device_ip", "nic_ip", "subnet_ip"]


async def test_pc_shows_only_applicable_relations(client, auth_headers, db_session) -> None:
    net = await _net(db_session, "10.30.0.0/24")
    pc = Device(name=_u("acct-pc-07"), type="workstation")
    db_session.add(pc)
    await db_session.flush()
    await _ip(db_session, net, "10.30.0.7", device_id=pc.id)
    await db_session.commit()
    d = await _page(client, auth_headers)
    f = d["focus"]
    types = sorted(n["type"] for n in f["nodes"])
    assert types == ["device", "ip", "section", "subnet"]               # 沒有假的機櫃、VM
    rows = {r["kind"]: r["state"] for r in f["rows"]}
    assert rows == {"physical": "ok", "virtual": "not_configured", "address": "ok"}
    assert d["metrics_state"]["virt"] == "not_configured"
    assert d["metrics"]["virt"] is None                                # 未設定，不是 0
    sec = next(n for n in f["nodes"] if n["type"] == "section")
    assert sec["sub"] is None                                          # 區段沒有 CIDR：只顯示名稱
    assert d["pending"]["device_no_place"] == 0                        # 個人電腦不算缺機櫃


# ─────────────────────────── 2. 焦點輪播 ───────────────────────────

async def test_focus_order_is_by_relation_kinds_and_stable(client, auth_headers, db_session) -> None:
    loc = Location(name=_u("新竹機房"))
    db_session.add(loc)
    await db_session.flush()
    rack = Rack(name=_u("A01"), location_id=loc.id)
    db_session.add(rack)
    await db_session.flush()
    racked = Device(name=_u("db-01"), type="server", rack_id=rack.id)
    loose = Device(name=_u("printer-3f"), type="other")
    db_session.add_all([racked, loose])
    await db_session.flush()
    net = await _net(db_session, "10.40.0.0/24")
    lone = await _ip(db_session, net, "10.40.0.1")                    # 沒有任何關係：不當焦點
    p1 = await _ip(db_session, net, "10.40.0.50", device_id=loose.id)
    r2 = await _ip(db_session, net, "10.40.0.200", device_id=racked.id)
    r1 = await _ip(db_session, net, "10.40.0.20", device_id=racked.id)
    await db_session.commit()
    order = []
    d = await _page(client, auth_headers)
    assert d["pages"] == 3
    assert d["total"] == 3
    for p in range(1, 4):
        order.append((await _page(client, auth_headers, p))["focus"]["ip"]["id"])
    # 裝置＋機櫃＋地點（3 種）在前，依位址；只有裝置的在後
    assert order == [str(r1.id), str(r2.id), str(p1.id)]
    assert str(lone.id) not in order
    from app.api.v1.endpoints import warroom
    warroom._cache.clear()
    again = [(await _page(client, auth_headers, p))["focus"]["ip"]["id"] for p in range(1, 4)]
    assert again == order                                              # 重新整理順序不變
    assert (await _page(client, auth_headers, 99))["page"] == 3         # 超出範圍夾回最後一頁


async def test_no_candidate_is_empty_not_zero_metrics(client, auth_headers, db_session) -> None:
    net = await _net(db_session, "10.50.0.0/24")
    await _ip(db_session, net, "10.50.0.1")
    await db_session.commit()
    d = await _page(client, auth_headers)
    assert d["focus"]["state"] == "empty"
    assert d["metrics"]["ips"] == 1


# ─────────────────────────── 3. 範圍與權限 ───────────────────────────

async def _overlap(db) -> dict:
    a, b = Customer(name=_u("cust-a")), Customer(name=_u("cust-b"))
    db.add_all([a, b])
    await db.flush()
    na = await _net(db, "10.0.0.0/24", customer=a)
    nb = await _net(db, "10.0.0.0/24", customer=b)
    da = Device(name=_u("a-server"), type="server")
    dbb = Device(name=_u("b-server"), type="server")
    db.add_all([da, dbb])
    await db.flush()
    ia = await _ip(db, na, "10.0.0.5", device_id=da.id)
    ib = await _ip(db, nb, "10.0.0.5", device_id=dbb.id)
    cl = VirtCluster(name=_u("b-cluster"), type="vmware", customer_id=b.id)
    db.add(cl)
    await db.flush()
    vm = VirtualMachine(cluster_id=cl.id, name=_u("b-vm"), node="esxi-b.example.test", status="running",
                        external_id="vm-5")
    db.add(vm)
    await db.flush()
    db.add(VMInterface(vm_id=vm.id, name="Network adapter 1", primary_ip="10.0.0.5"))
    await db.commit()
    return {"a": a, "b": b, "na": na, "nb": nb, "da": da, "db": dbb, "ia": ia, "ib": ib, "vm": vm}


async def test_overlapping_ranges_are_kept_apart(client, auth_headers, db_session) -> None:
    o = await _overlap(db_session)
    pages = (await _page(client, auth_headers))["pages"]
    focus = {}
    for p in range(1, pages + 1):
        f = (await _page(client, auth_headers, p))["focus"]
        focus[f["ip"]["id"]] = f
    assert set(focus) == {str(o["ia"].id), str(o["ib"].id)}            # 同一個位址字串，兩筆記錄
    fa, fb = focus[str(o["ia"].id)], focus[str(o["ib"].id)]
    assert f"vm:{o['vm'].id}" not in _keys(fa)                         # 另一個單位的 VM 不連到 A 的位址
    assert f"device:{o['db'].id}" not in _keys(fa)
    assert f"vm:{o['vm'].id}" in _keys(fb)
    e = next(e for e in fb["edges"] if e["kind"] == "nic_ip")
    assert e["line"] == "dashed"                                       # 只靠位址字串對到：推導


async def test_viewer_sees_only_permitted_scope(client, db_session) -> None:
    o = await _overlap(db_session)
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=o["na"].id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    d = await _page(client, _hdr(u))
    assert d["metrics"]["ips"] == 1
    assert d["metrics"]["virt"] is None                                 # 虛擬化是全域資料：None，不是 0
    assert d["metrics_state"]["virt"] == "forbidden"
    assert d["pending"]["vm_ip_unmapped"] is None
    assert d["pending"]["vm_no_report"] is None
    text = str(d)
    assert o["db"].name not in text
    assert o["vm"].name not in text
    assert str(o["ib"].id) not in text
    if d["focus"]["state"] == "ok":
        assert d["focus"]["ip"]["id"] == str(o["ia"].id)
        rows = {r["kind"]: r["state"] for r in d["focus"]["rows"]}
        assert rows["virtual"] == "forbidden"


async def test_display_scope_limits_focus(client, auth_headers, db_session) -> None:
    o = await _overlap(db_session)
    r = await client.put("/api/v1/system/warroom", headers=auth_headers,
                         json={"scope": {"subnets": [str(o["nb"].id)]}})
    assert r.status_code == 200, r.text
    d = await _page(client, auth_headers)
    assert d["metrics"]["ips"] == 1
    assert d["metrics"]["devices"] == 1
    assert d["pages"] == 1
    assert d["focus"]["ip"]["id"] == str(o["ib"].id)


# ─────────────────────────── 4. 關聯待補 ───────────────────────────

async def test_pending_counts_only_applicable_objects(client, auth_headers, db_session) -> None:
    loc = Location(name=_u("台中"))
    db_session.add(loc)
    await db_session.flush()
    net = await _net(db_session, "10.60.0.0/24")
    cl = VirtCluster(name=_u("pve"), type="proxmox")
    db_session.add(cl)
    await db_session.flush()
    devs = {
        "server_nowhere": Device(name=_u("app-01"), type="server"),             # 算
        "switch_with_loc": Device(name=_u("sw-01"), type="switch", location_id=loc.id),   # 有地點：不算
        "pc": Device(name=_u("pc-01"), type="workstation"),                      # PC：不算
        "printer": Device(name=_u("prn-01"), type="other"),                      # 印表機：不算
        "vm_as_device": Device(name="web-vm-07", type="server"),                 # 其實是 VM：不算
        "named": Device(name="nas-01", type="storage", location_id=loc.id),
        "dup1": Device(name="dup-host", type="other"),
        "dup2": Device(name="DUP-HOST", type="other"),
    }
    db_session.add_all(devs.values())
    await db_session.flush()
    # IP 沒連裝置但主機名稱只對到一台裝置：算；對到兩台：不猜、不算；完全沒有線索：不算
    await _ip(db_session, net, "10.60.0.10", hostname="nas-01.example.test")
    await _ip(db_session, net, "10.60.0.11", hostname="dup-host")
    await _ip(db_session, net, "10.60.0.12", hostname="random-laptop")
    vms = [
        VirtualMachine(cluster_id=cl.id, name="web-vm-07", status="running", legacy_vmid=1),
        VirtualMachine(cluster_id=cl.id, name=_u("unregistered"), status="running", legacy_vmid=2),
        VirtualMachine(cluster_id=cl.id, name=_u("no-agent"), status="running", legacy_vmid=3),
        VirtualMachine(cluster_id=cl.id, name=_u("stopped"), status="stopped", legacy_vmid=4),
        VirtualMachine(cluster_id=cl.id, name=_u("template"), status="stopped", legacy_vmid=5, is_template=True),
    ]
    db_session.add_all(vms)
    await db_session.flush()
    db_session.add_all([
        VMInterface(vm_id=vms[0].id, name="net0", primary_ip="10.60.0.10"),     # 有 IP 記錄
        VMInterface(vm_id=vms[1].id, name="net0", primary_ip="172.31.9.9"),     # IPAM 沒有：算
        VMInterface(vm_id=vms[2].id, name="net0", mac="52:54:00:00:00:03"),     # 執行中、沒回報：算（另一類）
        VMInterface(vm_id=vms[3].id, name="net0"),                              # 停機：不算
    ])
    await db_session.commit()
    p = (await _page(client, auth_headers))["pending"]
    assert p["state"] == "ok"
    assert p["device_no_place"] == 1
    assert p["ip_unlinked"] == 1
    assert p["vm_ip_unmapped"] == 1
    assert p["vm_no_report"] == 1
    assert p["conflicts"] == 0


# ─────────────────────────── 5. 來源不一致 → 待確認 ───────────────────────────

async def test_conflicting_sources_are_pending(client, auth_headers, db_session) -> None:
    l1, l2 = Location(name=_u("台北總部")), Location(name=_u("高雄分部"))
    db_session.add_all([l1, l2])
    await db_session.flush()
    rack = Rack(name=_u("R1-02"), location_id=l1.id)
    db_session.add(rack)
    await db_session.flush()
    linked = Device(name=_u("erp-01"), type="server", rack_id=rack.id, location_id=l2.id)
    other = Device(name=_u("erp-01-old"), type="server")
    db_session.add_all([linked, other])
    await db_session.flush()
    net = await _net(db_session, "10.70.0.0/24")
    ip = await _ip(db_session, net, "10.70.0.5", device_id=linked.id, mac="00:11:22:33:44:55")
    other.primary_ip_id = ip.id                                         # 另一台裝置說這是它的主要 IP
    cl = VirtCluster(name=_u("pve"), type="proxmox")
    db_session.add(cl)
    await db_session.flush()
    vm = VirtualMachine(cluster_id=cl.id, name=_u("erp-vm"), status="running", legacy_vmid=9)
    db_session.add(vm)
    await db_session.flush()
    db_session.add(VMInterface(vm_id=vm.id, name="net0", mac="52:54:00:99:99:99", primary_ip="10.70.0.5"))
    await db_session.commit()

    d = await _page(client, auth_headers)
    f = d["focus"]
    keys = _keys(f)
    assert len(keys) == len(set(keys))
    # 兩個地點都畫：機櫃的地點是一般的邊，裝置自己登記的地點是待確認
    assert f"location:{l1.id}" in keys
    assert f"location:{l2.id}" in keys
    edges = {(e["from"], e["to"]): e for e in f["edges"]}
    assert edges[(f"location:{l1.id}", f"rack:{rack.id}")]["state"] == "ok"
    dev_loc = edges[(f"location:{l2.id}", f"device:{linked.id}")]
    assert dev_loc["state"] == "pending"
    assert dev_loc["reason"] == "location_mismatch"
    # 兩台裝置都說是自己的：兩條都待確認，不挑一條
    ipk = f"ip:{ip.id}"
    assert edges[(f"device:{linked.id}", ipk)]["state"] == "pending"
    assert edges[(f"device:{other.id}", ipk)]["state"] == "pending"
    # 位址對到 VM 網卡但 MAC 不同
    nic_ip = next(e for e in f["edges"] if e["kind"] == "nic_ip")
    assert nic_ip["state"] == "pending"
    assert nic_ip["reason"] == "mac_mismatch"
    assert set(f["conflicts"]) == {"location_mismatch", "multiple_devices", "mac_mismatch"}
    assert d["pending"]["conflicts"] == 2                               # 地點不一致＋主要 IP 連到別台


# ─────────────────────────── 6. 沒有權限 ───────────────────────────

async def test_no_visibility_is_forbidden_not_zero(client, db_session) -> None:
    net = await _net(db_session, "10.80.0.0/24")
    dev = Device(name=_u("secret-db"), type="server")
    db_session.add(dev)
    await db_session.flush()
    await _ip(db_session, net, "10.80.0.1", device_id=dev.id)
    await db_session.commit()
    u = await _user(db_session)
    d = await _page(client, _hdr(u))
    assert d["metrics"] == {"ips": None, "devices": None, "virt": None}
    assert d["focus"]["state"] == "forbidden"
    assert d["pending"]["state"] == "forbidden"
    assert d["counts"]["state"] == "forbidden"
    assert "secret-db" not in str(d)


async def test_hidden_device_is_not_drawn(client, db_session) -> None:
    net = await _net(db_session, "10.90.0.0/24")
    loc = Location(name=_u("hidden-site"))
    db_session.add(loc)
    await db_session.flush()
    dev = Device(name=_u("hidden-server"), type="server", location_id=loc.id)
    db_session.add(dev)
    await db_session.flush()
    await _ip(db_session, net, "10.90.0.1", device_id=dev.id)
    await db_session.commit()
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=net.id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    d = await _page(client, _hdr(u))
    assert dev.name not in str(d)
    assert loc.name not in str(d)
