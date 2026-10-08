"""IP 變更評估 M2：交換器維護、虛擬化節點停機、服務依賴（規格 §6、T28–T33）。

拓樸（全部是實體佈線，A 那條經過跳接面板）：
    A ── P(front1/rear1) ── S          A 只接 S → 模型內中斷（T28）
    B ── S, B ── S2 ── C               B 另一條經 S2（S2 還接到 C）→ 備援待驗證（T29）
    D ── S, D ── X ── S                D 另一條經 X，但 X 只接 S → 共用上游，不算備援（T30）
"""
from __future__ import annotations

import uuid
from typing import Any

from app.models.change_impact import (
    ImpactDependencyGroup,
    ImpactDependencyMember,
    ImpactService,
)
from app.models.device import Device
from app.models.physical import Cable, CableTermination, DevicePort
from app.models.virt import VirtCluster, VirtualMachine
from app.services.change_impact.adapters_m2 import (
    AVAILABLE,
    UNAVAILABLE,
    UNKNOWN,
    _sccs,
    group_result,
)
from app.services.change_impact.engine import analyze
from sqlalchemy import text as text_

CFG: dict[str, Any] = {"limits": {}, "default_stale_hours": 24}


async def _dev(db, name: str, kind: str = "server") -> Device:  # type: ignore[no-untyped-def]
    d = Device(name=name, type=kind)
    db.add(d)
    await db.flush()
    return d


async def _port(db, dev: Device, name: str, ptype: str = "network", peer: DevicePort | None = None) -> DevicePort:  # type: ignore[no-untyped-def]
    p = DevicePort(device_id=dev.id, name=name, type=ptype, peer_port_id=peer.id if peer else None)
    db.add(p)
    await db.flush()
    return p


async def _cable(db, a: DevicePort, b: DevicePort) -> None:  # type: ignore[no-untyped-def]
    c = Cable(label=f"c-{uuid.uuid4().hex[:4]}", status="connected")
    db.add(c)
    await db.flush()
    db.add_all([CableTermination(cable_id=c.id, side="A", object_type="device_port", object_id=a.id),
                CableTermination(cable_id=c.id, side="B", object_type="device_port", object_id=b.id)])
    await db.flush()


async def _topology(db) -> dict[str, Device]:  # type: ignore[no-untyped-def]
    S = await _dev(db, "sw-core-01", "switch")
    S2 = await _dev(db, "sw-core-02", "switch")
    C = await _dev(db, "router-01", "router")
    X = await _dev(db, "sw-access-09", "switch")
    P = await _dev(db, "patch-01", "patch_panel")
    A = await _dev(db, "app-01")
    B = await _dev(db, "db-01")
    D = await _dev(db, "web-01")
    E = await _dev(db, "db-02")
    # A ─ P ─ S（跳接面板 front1 ↔ rear1 穿透）
    rear = await _port(db, P, "rear1", "rear")
    front = await _port(db, P, "front1", "front", peer=rear)
    rear.peer_port_id = front.id
    await _cable(db, await _port(db, A, "eth0"), front)
    await _cable(db, rear, await _port(db, S, "ge1"))
    # B ─ S、B ─ S2 ─ C
    await _cable(db, await _port(db, B, "eth0"), await _port(db, S, "ge2"))
    await _cable(db, await _port(db, B, "eth1"), await _port(db, S2, "ge1"))
    await _cable(db, await _port(db, S2, "ge48"), await _port(db, C, "p1"))
    # D ─ S、D ─ X ─ S
    await _cable(db, await _port(db, D, "eth0"), await _port(db, S, "ge3"))
    await _cable(db, await _port(db, D, "eth1"), await _port(db, X, "p1"))
    await _cable(db, await _port(db, X, "p24"), await _port(db, S, "ge24"))
    await db.commit()
    return {"S": S, "S2": S2, "C": C, "X": X, "P": P, "A": A, "B": B, "D": D, "E": E}


async def _service(db, name: str, groups: list[tuple[str, int, list[tuple[str, Any, str]]]],
                   confirmed: bool = True) -> ImpactService:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime
    svc = ImpactService(name=name, criticality="high")
    db.add(svc)
    await db.flush()
    for i, (gname, req, members) in enumerate(groups):
        g = ImpactDependencyGroup(service_id=svc.id, name=gname, required_count=req, position=i,
                                  confirmed_at=datetime.now(UTC) if confirmed else None)
        db.add(g)
        await db.flush()
        for otype, oid, rel in members:
            db.add(ImpactDependencyMember(group_id=g.id, object_type=otype, object_id=oid, relation_type=rel))
    await db.flush()
    return svc


async def _run(db, user, target: Device, scenario: str, **params: Any):  # type: ignore[no-untyped-def]
    await db.commit()
    return await analyze(db, user=user, scenario_type=scenario, target_type="device", target_id=target.id,
                         parameters=params, cfg=CFG)


def _by(res, rule: str) -> list[Any]:  # type: ignore[no-untyped-def]
    return [f for f in res.findings if f.rule_id == rule]


def _labels(res, rule: str) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.subject_label for f in _by(res, rule)}


async def test_t28_t29_t30_switch_maintenance(db_session, admin_user) -> None:
    t = await _topology(db_session)
    res = await _run(db_session, admin_user, t["S"], "switch_maintenance")
    assert _labels(res, "network.single_homed") == {"app-01"}            # T28，經跳接面板也找得到
    single = _by(res, "network.single_homed")[0]
    assert single.path and "patch-01" in str(single.path)                 # 附完整路徑
    assert _labels(res, "network.redundancy_unverified") == {"db-01"}     # T29
    # T30：web-01 的另一條路經過 sw-access-09，但它也只接回 sw-core-01；sw-access-09 自己也一樣
    assert _labels(res, "network.shared_upstream") == {"web-01", "sw-access-09"}
    gaps = {g.reason_code for g in res.gaps}
    assert {"network_state_unknown", "link_health_unknown"} <= gaps
    assert res.decision == "needs_review"


async def test_t33_multiple_targets_are_one_scenario(db_session, admin_user) -> None:
    t = await _topology(db_session)
    # S 與 S2 一起維護：db-01 兩條都斷 → 模型內中斷（不能把兩次單台的結果相加）
    res = await _run(db_session, admin_user, t["S"], "switch_maintenance", also_down=[str(t["S2"].id)])
    assert "db-01" in _labels(res, "network.single_homed")


def test_t31_k_of_n_is_three_valued() -> None:
    assert group_result([AVAILABLE, UNKNOWN, UNAVAILABLE], 2) == UNKNOWN
    assert group_result([AVAILABLE, AVAILABLE, UNKNOWN], 2) == AVAILABLE
    assert group_result([UNAVAILABLE, UNKNOWN], 2) == UNAVAILABLE
    assert group_result([UNKNOWN], 1) == UNKNOWN


def test_cycles_are_found_without_recursing_forever() -> None:
    comps = _sccs(["a", "b", "c", "d"], {"a": {"b"}, "b": {"a"}, "c": {"a"}})
    assert sorted(sorted(c) for c in comps) == [["a", "b"], ["c"], ["d"]]
    # 依賴的元件先回傳：a/b 在 c 之前
    order = [sorted(c) for c in comps]
    assert order.index(["a", "b"]) < order.index(["c"])


async def test_services_follow_dependency_groups(db_session, admin_user) -> None:
    t = await _topology(db_session)
    erp = await _service(db_session, "ERP", [("app", 1, [("device", t["A"].id, "requires_network")])])
    web = await _service(db_session, "Web", [("db", 1, [("device", t["B"].id, "requires_service"),
                                                         ("device", t["E"].id, "requires_service")])])
    rpt = await _service(db_session, "Reports", [("db", 2, [("device", t["B"].id, "requires_network"),
                                                             ("device", t["E"].id, "requires_network"),
                                                             ("device", t["A"].id, "requires_network")])])
    note = await _service(db_session, "Wiki", [("ref", 1, [("device", t["A"].id, "references")])])
    res = await _run(db_session, admin_user, t["S"], "switch_maintenance")
    assert "ERP" in _labels(res, "service.modeled_disruption")
    assert "Web" in _labels(res, "service.dependencies_hold")            # 1-of-2，另一台可用
    assert "Reports" in _labels(res, "service.redundancy_unverified")    # 2-of-3 含未知 → 未知（T31）
    names = {f.subject_label for f in res.findings if f.rule.category == "service"}
    assert "Wiki" not in names                                          # references 不傳播停機
    assert {erp.name, web.name, rpt.name} <= names and note.name not in names


async def test_t33_service_cycles_are_unknown_and_reported(db_session, admin_user) -> None:
    t = await _topology(db_session)
    x = await _service(db_session, "Auth", [("net", 1, [("device", t["A"].id, "requires_network")])])
    y = await _service(db_session, "Directory", [("auth", 1, [("service", x.id, "requires_service")])])
    db_session.add(ImpactDependencyGroup(service_id=x.id, name="dir", required_count=1))
    await db_session.flush()
    gid = (await db_session.execute(
        __import__("sqlalchemy").text("SELECT id FROM impact_dependency_groups WHERE service_id = :s AND name = 'dir'"),
        {"s": x.id})).scalar()
    db_session.add(ImpactDependencyMember(group_id=gid, object_type="service", object_id=y.id,
                                          relation_type="requires_service"))
    res = await _run(db_session, admin_user, t["S"], "switch_maintenance")
    assert {"Auth", "Directory"} <= _labels(res, "service.cycle")
    assert "service_dependency_cycle" in {g.reason_code for g in res.gaps}


async def test_t32_node_downtime_modes(db_session, admin_user) -> None:
    node = await _dev(db_session, "pve-01")
    cl = VirtCluster(name=f"cl-{uuid.uuid4().hex[:6]}")
    db_session.add(cl)
    await db_session.flush()
    vm1 = VirtualMachine(cluster_id=cl.id, name="erp-vm", node="pve-01", status="running", kind="vm")
    vm2 = VirtualMachine(cluster_id=cl.id, name="old-vm", node="pve-01", status="stopped", kind="vm")
    db_session.add_all([vm1, vm2])
    await db_session.flush()
    await _service(db_session, "ERP", [("vm", 1, [("vm", vm1.id, "hosted_on")])])
    res = await _run(db_session, admin_user, node, "node_downtime")
    assert any("erp-vm" in f.subject_label for f in _by(res, "workload.vm_stops"))
    assert any("old-vm" in f.subject_label for f in _by(res, "workload.vm_stopped"))
    assert "ERP" in _labels(res, "service.modeled_disruption")
    # 交給 HA：有設定不等於會恢復 → 備援待驗證＋條件缺口，不推算 RTO
    res = await _run(db_session, admin_user, node, "node_downtime", mode="ha_failure")
    assert any("erp-vm" in f.subject_label for f in _by(res, "workload.vm_ha_unverified"))
    assert "ha_conditions_unknown" in {g.reason_code for g in res.gaps}
    assert "ERP" in _labels(res, "service.redundancy_unverified")
    # 先遷移再停機：計畫遷移不等於已經離開節點
    res = await _run(db_session, admin_user, node, "node_downtime", mode="migrate_first")
    assert "migration_not_verified" in {g.reason_code for g in res.gaps}
    assert any("erp-vm" in f.subject_label for f in _by(res, "workload.vm_stops"))


async def test_services_need_global_read(client, db_session) -> None:
    """服務是共享基礎設施：只有某台裝置權限的人分析維護，不會看到服務結果，而是 permission_limited。"""
    from app.models.permission import Permission

    from tests.test_change_impact_api import _user
    t = await _topology(db_session)
    await _service(db_session, "ERP", [("app", 1, [("device", t["A"].id, "requires_network")])])
    u = await _user(db_session)
    db_session.add(Permission(object_type="device", object_id=t["S"].id, principal_type="user", principal_id=u.id,
                              level="write"))
    await db_session.commit()
    res = await _run(db_session, u, t["S"], "switch_maintenance")
    assert not [f for f in res.findings if f.rule.category == "service"]
    assert any(g.reason_code == "permission_limited" and g.category == "service" for g in res.gaps)


async def test_relation_graph_is_bounded_and_filtered(client, auth_headers, db_session, monkeypatch) -> None:
    """關係圖：交換器維護的根目標、受影響的裝置、服務都在；邊連到實際接的交換器與服務依賴的物件；只看得到裝置的人看不到服務。"""
    from app.models.permission import Permission
    from app.services.change_impact import ai as impact_ai
    from app.services.change_impact import jobs

    from tests.test_change_impact_api import _enable, _hdr, _user
    monkeypatch.setattr(jobs, "SPAWN_IN_BACKGROUND", False)
    monkeypatch.setattr(impact_ai, "SPAWN_IN_BACKGROUND", False)
    await _enable(db_session)
    t = await _topology(db_session)
    await _service(db_session, "ERP", [("app", 1, [("device", t["A"].id, "requires_network")])])
    await db_session.commit()
    r = await client.post("/api/v1/change-plans", headers=auth_headers, json={
        "title": "m", "scenario_type": "switch_maintenance", "target_type": "device", "target_id": str(t["S"].id)})
    pid = r.json()["id"]
    run = (await client.post(f"/api/v1/change-plans/{pid}/runs", headers=auth_headers)).json()
    g = (await client.get(f"/api/v1/impact-runs/{run['id']}/relations", headers=auth_headers)).json()
    labels = {n["label"] for n in g["nodes"]}
    assert {"sw-core-01", "app-01", "ERP"} <= labels
    root = next(n for n in g["nodes"] if n["root"])
    assert root["label"] == "sw-core-01"
    # 發現的主體帶分類（關係圖把同一類的葉節點收成群組要用）；只是路徑上的物件沒有分類
    app = next(n for n in g["nodes"] if n["label"] == "app-01")
    assert app["category"] and all("category" in n for n in g["nodes"])
    assert root["category"] is None
    assert any(e["from"] == f"device:{t['A'].id}" and e["to"] == root["id"] for e in g["edges"])
    assert any(e["to"] == f"device:{t['A'].id}" and e["relation"] == "requires_network" for e in g["edges"])
    u = await _user(db_session)
    for d in ("S", "A"):
        db_session.add(Permission(object_type="device", object_id=t[d].id, principal_type="user",
                                  principal_id=u.id, level="read"))
    await db_session.commit()
    g = (await client.get(f"/api/v1/impact-runs/{run['id']}/relations", headers=_hdr(u))).json()
    labels = {n["label"] for n in g["nodes"]}
    assert "app-01" in labels and "ERP" not in labels and "db-01" not in labels


# ─────────────────── FDB 推定：只看目前有效的條目、上行埠以「交換器＋埠」判斷 ───────────────────

async def _fdb_host(db, sw: Device, port: str, mac: str, ip: str, *, age_hours: float = 1) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from app.models.address import IPAddress
    from app.models.librenms import FDBEntry
    from app.models.section import Section
    from app.models.subnet import Subnet
    sub = (await db.execute(text_("SELECT id FROM subnets WHERE cidr = '198.51.100.0/24' LIMIT 1"))).scalar()
    if sub is None:
        sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
        db.add(sec)
        await db.flush()
        s = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
        db.add(s)
        await db.flush()
        sub = s.id
    seen = datetime.now(UTC) - timedelta(hours=age_hours)
    db.add(FDBEntry(mac=mac, port_name=port, source="mikrotik", switch_device_id=sw.id,
                    first_seen_at=seen - timedelta(days=1), last_seen_at=seen))
    db.add(IPAddress(subnet_id=sub, ip=ip, state="active", mac=mac))
    await db.flush()


async def test_fdb_entries_no_longer_current_are_not_inferred(db_session, admin_user) -> None:
    """FDB 保留一年的歷史；一個月前在這台交換器上看到的機器，現在不一定還接在這裡。"""
    sw = await _dev(db_session, "sw-fdb-01", "switch")
    await _fdb_host(db_session, sw, "7", "00:00:5e:00:53:71", "198.51.100.71")
    await _fdb_host(db_session, sw, "8", "00:00:5e:00:53:72", "198.51.100.72", age_hours=24 * 30)
    res = await _run(db_session, admin_user, sw, "switch_maintenance")
    got = {f.params["mac"] for f in res.findings if f.rule_id == "network.fdb_inferred"}
    assert got == {"00:00:5e:00:53:71"}


async def test_same_port_name_on_two_switches_is_not_one_uplink(db_session, admin_user) -> None:
    """兩台一起維護、都有叫「1」的埠：各 20 個 MAC 不是一個 40 個 MAC 的上行埠。"""
    a = await _dev(db_session, "sw-fdb-a", "switch")
    b = await _dev(db_session, "sw-fdb-b", "switch")
    for i in range(20):
        await _fdb_host(db_session, a, "1", f"00:00:5e:00:5a:{i:02x}", f"198.51.100.{100 + i}")
        await _fdb_host(db_session, b, "1", f"00:00:5e:00:5b:{i:02x}", f"198.51.100.{150 + i}")
    res = await _run(db_session, admin_user, a, "switch_maintenance", also_down=[str(b.id)])
    assert "fdb_uplink_skipped" not in {g.reason_code for g in res.gaps}
    assert len([f for f in res.findings if f.rule_id == "network.fdb_inferred"]) == 40
