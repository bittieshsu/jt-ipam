"""戰情室第二、三批主題（2026-10-21）：03 拓樸、04 服務、05 異常、06 資產。

守的是（規範「資料正確性」）：
1. 每個來源的格數與計數一致；憑證到期區間互斥；彙整時標每格幾個
2. 管理員限定的資料（DNS 比對、憑證、異常、監控覆蓋、IP 變更評估）對非管理員是 forbidden、不帶數量
3. 沒有歷史的時段畫「無資料」，不畫成「沒有事件」
4. 拓樸只用看得到的裝置；線型照證據等級（宣告／監控是實線、學到／推導是虛線）
5. 升級後新完成的主題自動加進播放清單，但管理員自己拿掉的不會被加回來
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.address import IPAddress
from app.models.anomaly_observation import AnomalyObservation
from app.models.certificate import Certificate, CertVersion
from app.models.device import Device
from app.models.dns import DNSRecord, DNSServer, DNSZone
from app.models.dns_compare_group import DNSCompareGroup
from app.models.permission import Permission
from app.models.physical import Cable, CableTermination
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from app.models.virt import VirtCluster, VirtualMachine
from app.services import dns_compare

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


async def _page(client, headers, key: str, page: int = 1) -> dict:
    r = await client.get(f"/api/v1/warroom/pages/{key}?page={page}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ─────────────────────────── 04 服務 ───────────────────────────

async def _dns_group(db) -> tuple[DNSCompareGroup, list[DNSServer]]:
    now = datetime.now(UTC)
    g = DNSCompareGroup(name=f"Taipei forward and reverse {uuid.uuid4().hex[:4]}", grace_minutes=0)
    db.add(g)
    await db.flush()
    servers = []
    for name in ("dns-primary.production.internal.example.com", "dns-secondary.production.internal.example.com"):
        s = DNSServer(name=f"{name}-{uuid.uuid4().hex[:4]}", type="bind9", compare_group_id=g.id,
                      last_sync_at=now - timedelta(minutes=1))
        db.add(s)
        await db.flush()
        servers.append(s)
    # 兩台都有 a、b；只有第一台有 c（第二台缺 c）
    for s, names in ((servers[0], ("a", "b", "c")), (servers[1], ("a", "b"))):
        z = DNSZone(server_id=s.id, name="example.com", type="forward")
        db.add(z)
        await db.flush()
        for i, n in enumerate(names):
            db.add(DNSRecord(zone_id=z.id, name=n, type="A", value=f"192.0.2.{i + 1}", source="from_dns_pulled",
                             name_norm=dns_compare.normalize_name(n, "example.com"),
                             value_norm=dns_compare.normalize_value("A", f"192.0.2.{i + 1}")))
    await db.flush()
    await dns_compare.check_group(db, g, now=now + timedelta(minutes=5))
    await db.commit()
    return g, servers


async def _cert(db, name: str, days: float) -> None:
    c = Certificate(name=name)
    db.add(c)
    await db.flush()
    db.add(CertVersion(certificate_id=c.id, fingerprint_sha256=uuid.uuid4().hex * 2, cert_pem="-", key_enc=b"x", key_nonce=b"y",
                       not_after=datetime.now(UTC) + timedelta(days=days), is_current=True))
    await db.flush()


async def test_services_dns_sources_certs_and_buckets(client, auth_headers, db_session) -> None:
    g, servers = await _dns_group(db_session)
    for name, days in (("expired.example.com", -2), ("customer-portal.production.ap-southeast.example.com", 6.5),
                       ("eight.example.com", 8.5), ("fourteen.example.com", 14.5), ("twenty.example.com", 20.5),
                       ("far.example.com", 200)):
        await _cert(db_session, name, days)
    await db_session.commit()

    d = await _page(client, auth_headers, "services")
    dns = d["dns"]
    assert dns["state"] == "ok"
    assert dns["group"]["name"] == g.name
    assert dns["group"]["status"] == "mismatch"
    src = {s["name"]: s for s in dns["group"]["sources"]}
    first, second = src[servers[0].name], src[servers[1].name]
    # 第一台：a、b 一致，c 只有它有；第二台：a、b 一致，缺 c（已確認）
    assert first["counts"] == {"ok": 2, "partial": 1, "missing": 0, "pending": 0}
    assert second["counts"] == {"ok": 2, "partial": 0, "missing": 1, "pending": 0}
    for s in (first, second):
        assert len(s["cells"]) == sum(s["counts"].values())       # 一格一筆、格數＝計數加總
        assert s["unit"] == 1
    assert sorted(second["cells"]) == ["missing", "ok", "ok"]

    certs = d["certs"]
    assert certs["state"] == "ok"
    # 區間互斥：已過期、7 天內、8 到 14 天、15 到 30 天；200 天的不算
    assert certs["buckets"] == {"expired": 1, "d7": 1, "d14": 2, "d30": 1}
    assert certs["item"]["name"] == "expired.example.com"         # 最急的先出現
    assert d["metrics"]["certs_30d"] == 5
    assert d["metrics"]["dns_mismatch"] == 1

    # 第二個子頁：憑證換下一張（DNS 只有一組就停在那一組）
    d2 = await _page(client, auth_headers, "services", 2)
    assert d2["certs"]["item"]["name"] == "customer-portal.production.ap-southeast.example.com"
    assert d2["dns"]["group"]["name"] == g.name
    assert d["pages"] >= 5


async def test_services_admin_data_forbidden_for_others(client, db_session) -> None:
    await _dns_group(db_session)
    await _cert(db_session, "soon.example.com", 3)
    await db_session.commit()
    u = await _user(db_session)
    d = await _page(client, _hdr(u), "services")
    assert d["dns"] == {"state": "forbidden"}
    assert d["certs"] == {"state": "forbidden"}
    assert d["metrics"]["dns_mismatch"] is None
    assert d["metrics"]["certs_30d"] is None


# ─────────────────────────── 05 異常 ───────────────────────────

async def test_anomaly_timeline_and_priority(client, auth_headers, db_session) -> None:
    now = datetime.now(UTC)
    rows = [
        # 兩小時前出現、還沒消失
        AnomalyObservation(category="ip_conflicts", fingerprint="a" * 16, label="10.20.10.58", ip="10.20.10.58",
                           first_seen_at=now - timedelta(hours=2), last_seen_at=now),
        # 一小時前出現、半小時前消失
        AnomalyObservation(category="unauthorized_ips", fingerprint="b" * 16, label="10.30.20.88",
                           ip="10.30.20.88", first_seen_at=now - timedelta(hours=1),
                           last_seen_at=now - timedelta(minutes=30), resolved_at=now - timedelta(minutes=30)),
        AnomalyObservation(category="dangling_dns", fingerprint="c" * 16,
                           label="customer-portal.production.ap-southeast.example.com",
                           first_seen_at=now - timedelta(minutes=10), last_seen_at=now),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    d = await _page(client, auth_headers, "anomalies")
    tl = d["timeline"]
    assert tl["state"] == "ok"
    assert tl["slots"] == 18 and tl["slot_minutes"] == 20
    by = {r["category"]: r for r in tl["rows"]}
    conflicts = by["ip_conflicts"]
    assert conflicts["open"] == 1
    assert len(conflicts["cells"]) == 18
    # 觀測資料從兩小時前才開始：之前的時段是「無資料」，不是「沒有事件」
    first_data = 18 - 6                                            # 最後 6 格（2 小時）有資料
    assert conflicts["cells"][:first_data - 1] == ["nodata"] * (first_data - 1)
    assert "new" in conflicts["cells"]
    assert conflicts["cells"][-1] == "active"
    unauth = by["unauthorized_ips"]
    assert unauth["open"] == 0
    assert "resolved" in unauth["cells"]                           # 這段期間出現過、後來消失
    assert unauth["cells"][-1] == "quiet"                           # 已經消失：最後一格沒有事件
    assert tl["data_since"] is not None

    pr = d["priority"]
    assert pr["state"] == "ok"
    assert pr["total"] == 2                                       # 只列還沒消失的
    assert [i["category"] for i in pr["items"]] == ["ip_conflicts", "dangling_dns"]   # 高嚴重度在前
    assert d["metrics"]["open_anomalies"] == 2
    assert d["metrics"]["resolved_6h"] == 1


async def test_anomalies_present_before_observation_began_are_not_new(client, auth_headers, db_session) -> None:
    """觀測開始那一刻就已經存在的（baseline）：時序畫「持續中」、不算進「6 小時內新出現」、清單不寫成那時才首次出現
    （10-22 部署 0224 後 04:02 那格 149 筆全是「新出現」，上方也寫新出現 149）。"""
    now = datetime.now(UTC)
    since = now - timedelta(hours=3, minutes=50)
    db_session.add_all([
        AnomalyObservation(category="unauthorized_ips", fingerprint="d" * 16, label="10.30.20.90", ip="10.30.20.90",
                           first_seen_at=since, last_seen_at=now, baseline=True),
        AnomalyObservation(category="unauthorized_ips", fingerprint="e" * 16, label="10.30.20.91", ip="10.30.20.91",
                           first_seen_at=now - timedelta(minutes=30), last_seen_at=now),
    ])
    await db_session.commit()
    d = await _page(client, auth_headers, "anomalies")
    cells = d["timeline"]["rows"][0]["cells"]
    first = next(i for i, c in enumerate(cells) if c != "nodata")
    assert cells[first] == "active"                                   # 開始記錄那格：已經存在，不是新出現
    assert cells.count("new") == 1                                    # 只有 30 分鐘前那筆
    assert d["metrics"]["new_6h"] == 1
    items = {i["label"]: i for i in d["priority"]["items"]}
    assert items["10.30.20.90"]["baseline"] is True
    assert items["10.30.20.91"]["baseline"] is False


async def test_anomalies_forbidden_for_non_admin(client, db_session) -> None:
    u = await _user(db_session)
    d = await _page(client, _hdr(u), "anomalies")
    assert d["timeline"] == {"state": "forbidden"}
    assert d["priority"] == {"state": "forbidden"}
    assert d["metrics"] is None


# ─────────────────────────── 06 資產 ───────────────────────────

async def test_assets_clusters_hosts_and_change_assessment(client, auth_headers, db_session) -> None:
    pve = VirtCluster(name=f"taipei-nangang-production-{uuid.uuid4().hex[:4]}", type="proxmox")
    hv = VirtCluster(name=f"Singapore Regional Virtualization Cluster {uuid.uuid4().hex[:4]}", type="hyper-v")
    db_session.add_all([pve, hv])
    await db_session.flush()
    for i in range(5):
        db_session.add(VirtualMachine(cluster_id=pve.id, name=f"vm{i}", node="node-01" if i < 3 else "node-02",
                                      status="running" if i < 4 else "stopped"))
    db_session.add(VirtualMachine(cluster_id=pve.id, name="tmpl", node="node-01", status="stopped", is_template=True))
    db_session.add(VirtualMachine(cluster_id=hv.id, name="hv1", node="HV-01", status="unknown"))
    await db_session.commit()

    d = await _page(client, auth_headers, "assets")
    assert d["metrics"]["hosts"] == 3                              # 叢集內不重複的節點
    assert d["metrics"]["vms"] == 6                                # 範本不算
    cl = {c["name"]: c for c in d["clusters"]}
    p = cl[pve.name]
    assert p["platform"] == "proxmox"
    assert p["hosts"] == 2
    assert p["counts"] == {"running": 4, "other": 0, "stopped": 1, "unknown": 0}
    assert sorted(p["cells"]) == ["running"] * 4 + ["stopped"]
    assert cl[hv.name]["counts"]["unknown"] == 1
    assert d["clusters"][0]["name"] == pve.name                    # VM 多的在前
    assert d["change"]["state"] == "disabled"                      # IP 變更評估預設關閉


async def test_assets_virtualization_needs_global_read(client, db_session) -> None:
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    db_session.add(sub)
    await db_session.flush()
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=sub.id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    d = await _page(client, _hdr(u), "assets")
    assert d["clusters_state"] == "forbidden"                      # 只被指派單一子網路：看不到全域的虛擬化
    assert d["coverage"] == {"state": "forbidden"}
    assert d["change"] == {"state": "forbidden"}


# ─────────────────────────── 03 拓樸 ───────────────────────────

async def _cable(db, a: Device, b: Device) -> None:
    c = Cable(label=f"patch-{uuid.uuid4().hex[:4]}")
    db.add(c)
    await db.flush()
    db.add_all([CableTermination(cable_id=c.id, side="A", object_type="device", object_id=a.id),
                CableTermination(cable_id=c.id, side="B", object_type="device", object_id=b.id)])


async def test_topology_centres_on_the_busiest_device(client, auth_headers, db_session) -> None:
    fw = Device(name="Taipei core firewall", type="firewall")
    core = Device(name="taipei-nangang-core-switch-primary", type="switch")
    access = [Device(name=f"access-{i}", type="switch") for i in range(3)]
    hosts = [Device(name=f"host-{i}", type="server") for i in range(4)]
    db_session.add_all([fw, core, *access, *hosts])
    await db_session.flush()
    await _cable(db_session, fw, core)
    for a in access:
        await _cable(db_session, core, a)
    for h in hosts:
        await _cable(db_session, access[0], h)
    await db_session.commit()

    d = await _page(client, auth_headers, "topology")
    t = d["focus"]
    assert t["state"] == "ok"
    # 只畫網路設備（使用者 10-22）：伺服器不出現在圖上，只算成接在交換器下面的終端數
    assert t["device"]["name"] == "taipei-nangang-core-switch-primary"   # 網路設備之間連線最多（4 條）
    assert t["device"]["degree"] == 4
    assert [n["name"] for n in t["up"]] == ["Taipei core firewall"]
    down = {n["name"]: n for n in t["down"]}
    assert sorted(down) == [f"access-{i}" for i in range(3)]
    assert down["access-0"]["endpoints"] == 4                     # 底下 4 台伺服器只算數量
    assert "host-0" not in str(d)
    assert all(n["evidence"] == "asserted" and n["line"] == "solid" for n in t["up"] + t["down"])
    assert d["metrics"]["devices"] == 5                           # 防火牆＋4 台交換器


async def test_topology_only_uses_visible_devices(client, db_session) -> None:
    a = Device(name="visible-switch", type="switch")
    b = Device(name="hidden-switch", type="switch")
    db_session.add_all([a, b])
    await db_session.flush()
    await _cable(db_session, a, b)
    await db_session.commit()
    u = await _user(db_session)
    db_session.add(Permission(object_type="device", object_id=a.id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    d = await _page(client, _hdr(u), "topology")
    assert "hidden-switch" not in str(d)


# ─────────────────────────── 播放清單 ───────────────────────────

async def test_newly_ready_pages_join_the_playlist_once(client, auth_headers, db_session) -> None:
    from app.models.system_setting import SystemSetting
    # 舊版存的設定：當時只有三頁完成，管理員存了 overview＋collection
    db_session.add(SystemSetting(key="warroom", value={"pages": ["overview", "collection"]}))
    await db_session.commit()
    cfg = (await client.get("/api/v1/warroom/config", headers=auth_headers)).json()
    assert cfg["pages"] == ["overview", "topology", "services", "anomalies", "assets", "collection"]
    # 管理員再存一次（拿掉 assets）：之後不會被加回來
    r = await client.put("/api/v1/system/warroom", json={"pages": ["overview", "anomalies"]}, headers=auth_headers)
    assert r.json()["pages"] == ["overview", "anomalies"]
    cfg = (await client.get("/api/v1/warroom/config", headers=auth_headers)).json()
    assert cfg["pages"] == ["overview", "anomalies"]


async def test_playlist_order_is_kept(client, auth_headers, db_session) -> None:
    """播放清單可以拖拉排序（10-22 使用者：「清單要可以改變順序」）：存什麼順序就照什麼順序播。"""
    r = await client.put("/api/v1/system/warroom", json={"pages": ["collection", "overview", "anomalies"]},
                         headers=auth_headers)
    assert r.json()["pages"] == ["collection", "overview", "anomalies"]
    cfg = (await client.get("/api/v1/warroom/config", headers=auth_headers)).json()
    assert cfg["pages"] == ["collection", "overview", "anomalies"]


async def test_new_topic_joins_without_undoing_a_custom_order(client, auth_headers, db_session) -> None:
    """升級多了新主題時插在預設順序的前一個主題後面，不可以把整個清單重排回預設順序。"""
    from app.models.system_setting import SystemSetting
    from app.services.warroom import config as wcfg
    seen = [p for p in wcfg.READY_PAGES if p != "assets"]
    db_session.add(SystemSetting(key="warroom", value={"pages": ["collection", "overview"], "ready_seen": seen}))
    await db_session.commit()
    cfg = (await client.get("/api/v1/warroom/config", headers=auth_headers)).json()
    assert cfg["pages"] == ["collection", "overview", "assets"]


# ─────────────────────────── 顯示範圍 ───────────────────────────

async def _scoped_site(db) -> dict:
    from app.models.customer import Customer
    cust = Customer(name=f"cust-{uuid.uuid4().hex[:6]}")
    parent = Section(name=f"parent-{uuid.uuid4().hex[:6]}")
    db.add_all([cust, parent])
    await db.flush()
    child = Section(name=f"child-{uuid.uuid4().hex[:6]}", parent_id=parent.id)
    other_sec = Section(name=f"other-{uuid.uuid4().hex[:6]}")
    db.add_all([child, other_sec])
    await db.flush()
    in_child = Subnet(section_id=child.id, cidr="198.51.100.0/24")
    by_cust = Subnet(section_id=other_sec.id, cidr="203.0.113.0/24", customer_id=cust.id)
    outside = Subnet(section_id=other_sec.id, cidr="192.0.2.0/24")
    db.add_all([in_child, by_cust, outside])
    await db.flush()
    now = datetime.now(UTC)
    for i, ip in enumerate(("198.51.100.9", "192.0.2.9")):
        db.add(AnomalyObservation(category="ip_conflicts", fingerprint=f"{i}" * 16, label=ip, ip=ip,
                                  first_seen_at=now - timedelta(minutes=30), last_seen_at=now))
    db.add(AnomalyObservation(category="dangling_dns", fingerprint="d" * 16, label="old.example.com",
                              first_seen_at=now - timedelta(minutes=30), last_seen_at=now))
    await db.commit()
    return {"cust": cust, "parent": parent, "in_child": in_child, "by_cust": by_cust, "outside": outside}


async def test_display_scope_limits_subnet_pages(client, auth_headers, db_session) -> None:
    s = await _scoped_site(db_session)
    # 選上層區段（含子區段）＋一個單位：outside 那段不在範圍
    r = await client.put("/api/v1/system/warroom", headers=auth_headers, json={
        "scope": {"sections": [str(s["parent"].id)], "customers": [str(s["cust"].id)]}})
    assert r.status_code == 200, r.text
    labels = r.json()["scope_labels"]
    assert [x["label"] for x in labels["sections"]] == [s["parent"].name]
    cap = await _page(client, auth_headers, "capacity")
    assert sorted(i["ident"] for i in cap["items"]) == ["198.51.100.0/24", "203.0.113.0/24"]
    an = await _page(client, auth_headers, "anomalies")
    assert [i["label"] for i in an["priority"]["items"]] == ["198.51.100.9"]   # 範圍外與沒有位址的不列
    assert an["metrics"]["open_anomalies"] == 1
    ov = await _page(client, auth_headers, "overview")
    assert ov["metrics"]["open_anomalies"] == 1
    cfg = (await client.get("/api/v1/warroom/config", headers=auth_headers)).json()
    assert cfg["scope"]["names"]                                    # 頁首顯示部分範圍
    # 清空＝全部
    await client.put("/api/v1/system/warroom", headers=auth_headers, json={})
    cap = await _page(client, auth_headers, "capacity")
    assert "192.0.2.0/24" in [i["ident"] for i in cap["items"]]
    assert (await client.get("/api/v1/warroom/config", headers=auth_headers)).json()["scope"] is None


async def test_display_scope_never_widens_permissions(client, db_session, auth_headers) -> None:
    s = await _scoped_site(db_session)
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["outside"].id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    await client.put("/api/v1/system/warroom", headers=auth_headers,
                     json={"scope": {"subnets": [str(s["in_child"].id)]}})
    cap = await _page(client, _hdr(u), "capacity")
    assert cap["items"] == []                                       # 權限 ∩ 範圍 ＝ 空


async def test_scope_options_search(client, auth_headers, db_session) -> None:
    s = await _scoped_site(db_session)
    r = await client.get("/api/v1/system/warroom/scope-options?kind=subnets&q=203.0.113", headers=auth_headers)
    assert [x["value"] for x in r.json()] == [str(s["by_cust"].id)]
    r = await client.get(f"/api/v1/system/warroom/scope-options?kind=sections&q={s['parent'].name}",
                         headers=auth_headers)
    assert [x["label"] for x in r.json()] == [s["parent"].name]
    u = await _user(db_session)
    r = await client.get("/api/v1/system/warroom/scope-options?kind=subnets", headers=_hdr(u))
    assert r.status_code == 403


# ─────────────────── 戰情室檢視者角色（2026-10-22 使用者：要有適合戰情室的角色） ───────────────────

async def _warroom_viewer(db) -> User:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.models.user import Group, UserGroupMember
    from app.services.permission import WARROOM_ROLE, seed_default_roles
    await seed_default_roles(db)
    g = (await db.execute(select(Group).where(Group.name == WARROOM_ROLE))).scalar_one()
    assert g.is_builtin
    u = await _user(db)
    db.add(UserGroupMember(user_id=u.id, group_id=g.id))
    await db.commit()
    return u


async def test_warroom_viewer_role_sees_every_topic(client, db_session) -> None:
    db_session.add(AnomalyObservation(category="ip_conflicts", fingerprint="r" * 16, label="198.51.100.9",
                                      ip="198.51.100.9", first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC)))
    await _cert(db_session, "soon.example.com", 3)
    await db_session.commit()
    u = await _warroom_viewer(db_session)
    an = await _page(client, _hdr(u), "anomalies")
    assert an["priority"]["state"] == "ok" and an["metrics"]["open_anomalies"] == 1
    sv = await _page(client, _hdr(u), "services")
    assert sv["certs"]["state"] == "ok"
    col = await _page(client, _hdr(u), "collection")
    assert col["agents"]["state"] != "forbidden"
    assets = await _page(client, _hdr(u), "assets")
    assert assets["clusters_state"] != "forbidden" and assets["coverage"]["state"] != "forbidden"
    ov = await _page(client, _hdr(u), "overview")
    assert ov["priority"]["state"] != "forbidden"


async def test_warroom_viewer_role_grants_nothing_outside_the_war_room(client, db_session) -> None:
    u = await _warroom_viewer(db_session)
    for path in ("/api/v1/anomalies/last", "/api/v1/certificates", "/api/v1/dns/compare-groups", "/api/v1/users"):
        r = await client.get(path, headers=_hdr(u))
        assert r.status_code == 403, (path, r.status_code)
    # 唯讀：不能改東西
    r = await client.post("/api/v1/sections", json={"name": "x"}, headers=_hdr(u))
    assert r.status_code == 403
