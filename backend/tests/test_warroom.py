"""戰情室（2026-10-21）：頁面資料、權限與電視用的顯示權杖。

守的是：
1. 格數、填色比例、分類計數與總數一致；彙整時標示每格代表幾個（規範「資料正確性」）
2. 權限照現有 RBAC：只看得到被指派的子網路；管理員限定的資料（異常、採集）對非管理員是 forbidden、不帶數量
3. 顯示權杖：預設關閉；只能讀戰情室端點；過期、撤銷、代表帳號停用都立即失效
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from app.models.address import IPAddress
from app.models.anomaly_observation import AnomalyObservation
from app.models.location import Location
from app.models.permission import Permission
from app.models.scan_agent import ScanAgent
from app.models.scan_agent_cycle import ScanAgentCycle
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User

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


async def _user(db, *, admin: bool = False) -> User:
    from app.core.security import hash_password
    u = User(username=f"u-{uuid.uuid4().hex[:8]}", email=f"u-{uuid.uuid4().hex[:8]}@test.local",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True, is_admin=admin)
    db.add(u)
    await db.commit()
    return u


async def _site(db) -> dict[str, Any]:
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    north = Location(name=f"North Campus {uuid.uuid4().hex[:4]}")
    south = Location(name=f"South Campus {uuid.uuid4().hex[:4]}")
    db.add_all([sec, north, south])
    await db.flush()
    agent = ScanAgent(name=f"scanner-production-ap-southeast-01-{uuid.uuid4().hex[:4]}",
                      enroll_key_hash=uuid.uuid4().hex * 2, enabled=True, last_seen_at=datetime.now(UTC))
    db.add(agent)
    await db.flush()
    full = Subnet(section_id=sec.id, cidr="198.51.100.0/24", description="Research clients",
                  location_id=north.id, scan_agent_id=agent.id)
    big = Subnet(section_id=sec.id, cidr="10.40.0.0/22", description="Production", location_id=north.id,
                 scan_agent_id=agent.id)
    other = Subnet(section_id=sec.id, cidr="203.0.113.0/24", location_id=south.id)
    db.add_all([full, big, other])
    await db.flush()
    # full：240 筆（10 筆保留）／254 ＝ 94.5%；big：100 筆；other：沒有記錄
    for i in range(1, 241):
        db.add(IPAddress(subnet_id=full.id, ip=f"198.51.100.{i}", state="reserved" if i > 230 else "active"))
    for i in range(100):
        db.add(IPAddress(subnet_id=big.id, ip=f"10.40.{i // 250}.{i % 250 + 1}", state="active"))
    await db.commit()
    return {"sec": sec, "north": north, "south": south, "full": full, "big": big, "other": other, "agent": agent}


async def test_capacity_cells_match_the_counts(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    d = (await client.get("/api/v1/warroom/pages/capacity", headers=auth_headers)).json()
    assert d["metrics"]["registered_ips"] == 340
    assert d["metrics"]["high_util_subnets"] == 1
    first = d["items"][0]
    assert first["ident"] == "198.51.100.0/24"                      # 配置率最高的先出現
    assert first["pct"] == 94.5
    assert first["level"] == "crit"
    assert first["order"] == "address"
    assert first["unit"] == 1
    cells = first["cells"]
    assert len(cells) == 254
    assert cells.count("allocated") == 230
    assert cells.count("reserved") == 10
    assert cells.count("free") == 14
    assert cells[0] == "allocated"                                   # 照位址順序：.1 在第一格
    assert cells[-1] == "free"                                       # .254
    big = next(i for i in d["items"] if i["ident"] == "10.40.0.0/22")
    assert big["order"] == "state"
    assert big["unit"] == 4                                          # 1022 個位址 → 每格 4 個、256 格
    assert len(big["cells"]) == 256
    assert big["cells"].count("allocated") == 25                     # 100 / 1022 ≈ 9.8%
    assert s["other"].id                                             # 沒有記錄的子網路也在清單裡（0%）
    assert any(i["ident"] == "203.0.113.0/24" and i["pct"] == 0 for i in d["items"])


async def test_overview_groups_by_location_and_marks_anomalies_for_admins(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    db_session.add(AnomalyObservation(category="ip_conflicts", fingerprint="a" * 16, label="198.51.100.5",
                                      ip="198.51.100.5", first_seen_at=datetime.now(UTC),
                                      last_seen_at=datetime.now(UTC)))
    await db_session.commit()
    d = (await client.get("/api/v1/warroom/pages/overview", headers=auth_headers)).json()
    assert d["metrics"]["open_anomalies"] == 1
    assert d["metrics"]["capacity_alerts"] == 1
    north = next(g for g in d["groups"] if g["name"] == s["north"].name)
    assert north["counts"] == {"ok": 1, "warn": 0, "anomaly": 1, "stale": 0, "unscanned": 0}
    assert sorted(north["cells"]) == ["anomaly", "ok"]
    south = next(g for g in d["groups"] if g["name"] == s["south"].name)
    assert south["counts"]["unscanned"] == 1                         # 沒有掃描代理＝未掃描，不是正常
    assert d["groups"][0]["name"] == s["north"].name                # 需要注意的站台先出現
    assert d["priority"]["state"] == "ok"
    assert d["priority"]["items"][0]["value"] == "198.51.100.5"


async def test_stale_agent_marks_its_subnets(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    await db_session.execute(update(ScanAgent).where(ScanAgent.id == s["agent"].id).values(
        last_seen_at=datetime.now(UTC) - timedelta(hours=2)))
    await db_session.commit()
    d = (await client.get("/api/v1/warroom/pages/overview", headers=auth_headers)).json()
    north = next(g for g in d["groups"] if g["name"] == s["north"].name)
    assert north["counts"]["warn"] == 1                              # 容量警戒優先於逾時
    assert north["counts"]["stale"] == 1
    cap = (await client.get("/api/v1/warroom/pages/capacity", headers=auth_headers)).json()
    assert all(i["scan"] == "stale" for i in cap["items"] if i["ident"] != "203.0.113.0/24")


async def test_non_admin_sees_only_assigned_subnets_and_no_admin_data(client, db_session) -> None:
    s = await _site(db_session)
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["other"].id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    d = (await client.get("/api/v1/warroom/pages/capacity", headers=_hdr(u))).json()
    assert [i["ident"] for i in d["items"]] == ["203.0.113.0/24"]
    assert d["metrics"]["registered_ips"] == 0
    ov = (await client.get("/api/v1/warroom/pages/overview", headers=_hdr(u))).json()
    assert ov["metrics"]["open_anomalies"] is None                   # 無權限：不帶數量
    assert ov["priority"] == {"state": "forbidden"}
    col = (await client.get("/api/v1/warroom/pages/collection", headers=_hdr(u))).json()
    assert col["agents"] == {"state": "forbidden"}
    assert col["integrations"] == {"state": "forbidden"}


async def test_collection_strip(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    now = datetime.now(UTC)
    for i, dur in enumerate((10, 40, 58)):
        db_session.add(ScanAgentCycle(agent_id=s["agent"].id, at=now - timedelta(minutes=3 - i), duration_s=dur,
                                      interval_s=60, hosts=10, alive=5))
    await db_session.commit()
    d = (await client.get("/api/v1/warroom/pages/collection", headers=auth_headers)).json()
    a = d["agents"]["items"][0]
    assert len(a["strip"]) == 24
    assert [c["state"] for c in a["strip"][:21]] == ["none"] * 21    # 不足的輪數補「無資料」，不畫成正常
    assert [c["state"] for c in a["strip"][21:]] == ["ok", "busy", "overloaded"]
    assert a["state"] == "overloaded"
    assert a["interval_s"] == 60


async def test_display_token(client, auth_headers, admin_user, db_session) -> None:
    s = await _site(db_session)
    viewer = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["full"].id, principal_type="user",
                              principal_id=viewer.id, level="read"))
    await db_session.commit()
    # 預設關閉：權杖不能用
    tok = (await client.post("/api/v1/system/warroom/token", json={"days": 30}, headers=auth_headers)).json()["token"]
    r = await client.get("/api/v1/warroom/pages/capacity", headers={"X-Warroom-Token": tok})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "warroom_token_disabled"
    # 打開時一定要指定代表帳號
    body = {"display_enabled": True}
    r = await client.put("/api/v1/system/warroom", json=body, headers=auth_headers)
    assert r.status_code == 422
    r = await client.put("/api/v1/system/warroom", json={**body, "principal_user_id": str(viewer.id)},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    assert "token" not in r.json()
    d = (await client.get("/api/v1/warroom/pages/capacity", headers={"X-Warroom-Token": tok})).json()
    assert [i["ident"] for i in d["items"]] == ["198.51.100.0/24"]  # 代表帳號的可見範圍
    cfg = (await client.get("/api/v1/warroom/config", headers={"X-Warroom-Token": tok})).json()
    assert cfg["via"] == "display"
    # 只能讀戰情室：別的 API 一律不認
    assert (await client.get("/api/v1/subnets", headers={"X-Warroom-Token": tok})).status_code == 401
    assert (await client.get("/api/v1/system/warroom", headers={"X-Warroom-Token": tok})).status_code == 401
    # 錯的權杖、停用代表帳號、撤銷
    assert (await client.get("/api/v1/warroom/config", headers={"X-Warroom-Token": "x" * 43})).status_code == 401
    await db_session.execute(update(User).where(User.id == viewer.id).values(is_active=False))
    await db_session.commit()
    r = await client.get("/api/v1/warroom/config", headers={"X-Warroom-Token": tok})
    assert r.json()["detail"]["code"] == "warroom_token_principal"
    await db_session.execute(update(User).where(User.id == viewer.id).values(is_active=True))
    await db_session.commit()
    assert (await client.delete("/api/v1/system/warroom/token", headers=auth_headers)).status_code == 204
    assert (await client.get("/api/v1/warroom/config", headers={"X-Warroom-Token": tok})).status_code == 401


async def test_settings_validation(client, auth_headers) -> None:
    r = await client.put("/api/v1/system/warroom", json={"capacity_warn": 90, "capacity_crit": 80}, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "warroom_thresholds"
    r = await client.put("/api/v1/system/warroom", json={"pages": ["capacity", "nope", "capacity"], "group_by": "x"},
                         headers=auth_headers)
    got = r.json()
    assert got["pages"] == ["capacity"]
    assert got["group_by"] == "location"


async def test_availability_window_setting(client, auth_headers) -> None:
    """可用性主題的時間範圍可以在戰情室設定選（10-23；以前 builder 會讀但設定存不進去，永遠是 7 天）。"""
    assert (await client.get("/api/v1/system/warroom", headers=auth_headers)).json()["availability_window"] == "7d"
    r = await client.put("/api/v1/system/warroom", json={"availability_window": "30d"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["availability_window"] == "30d"
    page = (await client.get("/api/v1/warroom/pages/availability", headers=auth_headers)).json()
    assert page["window"]["requested"] == "30d"
    # 不認得的值退回預設（跟 group_by 一樣）
    r = await client.put("/api/v1/system/warroom", json={"availability_window": "1y"}, headers=auth_headers)
    assert r.json()["availability_window"] == "7d"


async def test_transition_and_number_font_settings(client, auth_headers) -> None:
    """過場效果與大數字字體可在戰情室設定選（10-24）；電視拿到的設定也要帶，不認得的值退回預設。"""
    got = (await client.get("/api/v1/system/warroom", headers=auth_headers)).json()
    assert (got["transition"], got["number_font"]) == ("slide", "exo2")   # 預設 Exo 2
    r = await client.put("/api/v1/system/warroom", json={"transition": "zoom", "number_font": "orbitron"},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    tv = (await client.get("/api/v1/warroom/config", headers=auth_headers)).json()
    assert (tv["transition"], tv["number_font"]) == ("zoom", "orbitron")
    r = await client.put("/api/v1/system/warroom", json={"transition": "spin", "number_font": "comic"},
                         headers=auth_headers)
    assert (r.json()["transition"], r.json()["number_font"]) == ("slide", "exo2")


async def test_turning_the_war_room_off_blocks_signed_in_users(client, auth_headers) -> None:
    await client.put("/api/v1/system/warroom", json={"enabled": False}, headers=auth_headers)
    r = await client.get("/api/v1/warroom/config", headers=auth_headers)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "warroom_disabled"


async def test_pool_cards_are_titled_by_subnet_like_subnet_cards(client, auth_headers, db_session) -> None:
    """DHCP 集區卡片的標題跟子網路卡片一樣用子網路名稱，發放的伺服器另外放
    （10-22 使用者：「有的是顯示子網路名稱、有的是顯示閘道名稱」，DHCP 伺服器名稱被當成標題）。"""
    from app.models.dhcp import DHCPPoolRange
    s = await _site(db_session)
    db_session.add(DHCPPoolRange(source_type="mikrotik", source_id=uuid.uuid4(), source_name="gw-01",
                                 subnet_cidr="198.51.100.0/24", start_ip="198.51.100.150", end_ip="198.51.100.200",
                                 family=4, source="mikrotik"))
    await db_session.commit()
    items: list[dict] = []
    page = 1
    while True:
        d = (await client.get(f"/api/v1/warroom/pages/capacity?page={page}", headers=auth_headers)).json()
        items += d["items"]
        if page >= d["pages"]:
            break
        page += 1
    pool = next(i for i in items if i["kind"] == "pool")
    assert pool["label"] == s["full"].description == "Research clients"
    assert pool["server"] == "gw-01"
    svc = (await client.get("/api/v1/warroom/pages/services", headers=auth_headers)).json()
    assert svc["pool"]["item"]["label"] == "Research clients"
