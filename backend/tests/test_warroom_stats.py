"""戰情室 08 IP 統計（2026-10-23）。

守的是規格 §5：
1. 上線＋離線＋未知＝已登記 IP（每一個 effective_status 都有明確歸屬，含 online (arp) 與怪值）
2. 三列共用同一個「每格 N 個」尺度；尾數不足一格算一格；N 顯示在畫面上
3. 子網路排行依登記量排序、分頁，各頁加總＝總數（父子子網路不重複累加）
4. IPv4／IPv6 分開計數；稽核事件只有看得到稽核記錄的人才有數字（其他人是 None，不是 0）
5. 範圍：權限 ∩ 顯示範圍，所有數字同一個範圍
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.address import IPAddress
from app.models.permission import Permission
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from app.services.warroom.stats import ROW_CELLS, cell_unit, cells_for, status_class

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
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True)
    db.add(u)
    await db.commit()
    return u


# ───────────────────────────── 純函式 ─────────────────────────────

def test_every_status_value_maps_to_one_class() -> None:
    assert status_class("online") == "online"
    assert status_class("online (scanner)") == "online"
    assert status_class("online (arp)") == "online"          # 與儀表板一致；另外帶 arp_only 數
    assert status_class("Online (LibreNMS)") == "online"
    assert status_class("offline") == "offline"
    assert status_class("unknown") == "unknown"
    assert status_class(None) == "unknown"
    assert status_class("") == "unknown"
    assert status_class("maintenance") == "unknown"          # 不認得的值也有歸屬，不會從總數消失


def test_cell_unit_is_shared_and_readable() -> None:
    assert cell_unit(0) == 1
    assert cell_unit(ROW_CELLS) == 1
    assert cell_unit(ROW_CELLS + 1) == 2
    assert cell_unit(ROW_CELLS * 2 + 1) == 5
    assert cell_unit(ROW_CELLS * 5 + 1) == 10
    assert cell_unit(1_000_000) == 10_000
    for n in (1, 7, 161, 999, 12_345, 987_654):
        u = cell_unit(n)
        assert cells_for(n, u) <= ROW_CELLS


def test_partial_cell_counts_as_one() -> None:
    assert cells_for(0, 5) == 0
    assert cells_for(1, 5) == 1                               # 一筆離線不會因為尺度消失
    assert cells_for(5, 5) == 1
    assert cells_for(6, 5) == 2


# ───────────────────────────── 端點 ─────────────────────────────

async def _site(db) -> dict:
    sec_a = Section(name=f"core-{uuid.uuid4().hex[:6]}")
    sec_b = Section(name=f"branch-{uuid.uuid4().hex[:6]}")
    db.add_all([sec_a, sec_b])
    await db.flush()
    parent = Subnet(section_id=sec_a.id, cidr="10.20.0.0/16", description="Campus aggregate")
    child = Subnet(section_id=sec_a.id, cidr="10.20.1.0/24", description="Campus clients", master_subnet_id=None)
    v6 = Subnet(section_id=sec_b.id, cidr="2001:db8:1::/64", description="IPv6 servers")
    small = Subnet(section_id=sec_b.id, cidr="192.0.2.0/24")
    empty = Subnet(section_id=sec_b.id, cidr="198.51.100.0/24", description="Empty")
    db.add_all([parent, child, v6, small, empty])
    await db.flush()
    child.master_subnet_id = parent.id
    statuses = ["online", "online (scanner)", "online (arp)", "offline", "unknown", None, "maintenance"]
    # child：70 筆，狀態輪流；parent 直屬 5 筆（不含 child 的）；v6：9 筆；small：3 筆
    for i in range(70):
        db.add(IPAddress(subnet_id=child.id, ip=f"10.20.1.{i + 1}", effective_status=statuses[i % 7]))
    for i in range(5):
        db.add(IPAddress(subnet_id=parent.id, ip=f"10.20.9.{i + 1}", effective_status="online"))
    for i in range(9):
        db.add(IPAddress(subnet_id=v6.id, ip=f"2001:db8:1::{i + 1:x}", effective_status="offline"))
    for i in range(3):
        db.add(IPAddress(subnet_id=small.id, ip=f"192.0.2.{i + 1}", effective_status=None))
    await db.commit()
    return {"sec_a": sec_a, "sec_b": sec_b, "parent": parent, "child": child, "v6": v6, "small": small,
            "empty": empty}


async def _page(client, headers, page: int = 1) -> dict:
    r = await client.get(f"/api/v1/warroom/pages/stats?page={page}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_status_distribution_adds_up(client, auth_headers, db_session) -> None:
    await _site(db_session)
    d = await _page(client, auth_headers)
    m = d["metrics"]
    assert m["registered_ips"] == 87
    assert m["subnets"] == 5
    assert m["sections"] == 2
    st = d["status"]
    rows = {r["status"]: r for r in st["rows"]}
    # child 70 筆：每種 10 筆 → 上線 30（含 arp 10）、離線 10、未知 30（unknown、None、怪值）
    assert rows["online"]["count"] == 30 + 5
    assert rows["offline"]["count"] == 10 + 9
    assert rows["unknown"]["count"] == 30 + 3
    assert sum(r["count"] for r in st["rows"]) == st["total"] == m["registered_ips"]
    assert st["online_arp_only"] == 10
    assert [r["status"] for r in st["rows"]] == ["online", "offline", "unknown"]
    # 共用尺度：每格 1 個（最多那列 35 ≤ ROW_CELLS）
    assert st["unit"] == 1
    assert all(r["cells"] == r["count"] for r in st["rows"])
    assert d["families"] == {"v4": 78, "v6": 9}
    assert d["families"]["v4"] + d["families"]["v6"] == m["registered_ips"]


async def test_shared_scale_when_many(client, auth_headers, db_session) -> None:
    sec = Section(name=f"big-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    big = Subnet(section_id=sec.id, cidr="10.50.0.0/22")
    db_session.add(big)
    await db_session.flush()
    for i in range(400):
        db_session.add(IPAddress(subnet_id=big.id, ip=f"10.50.{i // 250}.{i % 250 + 1}",
                                 effective_status="offline" if i == 0 else "online"))
    await db_session.commit()
    st = (await _page(client, auth_headers))["status"]
    rows = {r["status"]: r for r in st["rows"]}
    assert st["unit"] == 5                                   # 399 → 每格 5 個，80 格
    assert rows["online"]["cells"] == 80
    assert rows["offline"]["cells"] == 1                     # 一筆也看得到
    assert rows["unknown"]["cells"] == 0
    assert rows["online"]["count"] == 399                    # 精確數字照樣帶


async def test_subnet_ranking_pages_add_up(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    d = await _page(client, auth_headers)
    assert d["per_page"] == 4
    items = d["ranking"]["items"]
    assert [i["cidr"] for i in items] == ["10.20.1.0/24", "2001:db8:1::/64", "10.20.0.0/16", "192.0.2.0/24"]
    assert [i["count"] for i in items] == [70, 9, 5, 3]      # 父網段只算直屬的 5 筆，不含子網段的 70 筆
    assert sum(i["count"] for i in items) == d["metrics"]["registered_ips"]
    assert items[0]["label"] == "Campus clients"
    assert items[0]["rank"] == 1
    assert all(i["cidr"] != str(s["empty"].cidr) for i in items)   # 沒有登記的子網路不進排行


async def test_ranking_paginates_without_double_counting(client, auth_headers, db_session) -> None:
    sec = Section(name=f"many-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    for n in range(12):
        sub = Subnet(section_id=sec.id, cidr=f"10.60.{n}.0/24", description=f"Floor {n}")
        db_session.add(sub)
        await db_session.flush()
        for i in range(n + 1):
            db_session.add(IPAddress(subnet_id=sub.id, ip=f"10.60.{n}.{i + 1}", effective_status="online"))
    await db_session.commit()
    first = await _page(client, auth_headers)
    assert first["pages"] == 3
    assert first["total"] == 12
    seen: list[str] = []
    total = 0
    for p in range(1, 4):
        d = await _page(client, auth_headers, p)
        assert len(d["ranking"]["items"]) <= 4
        seen += [i["cidr"] for i in d["ranking"]["items"]]
        total += sum(i["count"] for i in d["ranking"]["items"])
        assert d["status"]["total"] == first["status"]["total"]   # 分布固定對應完整範圍，不隨頁碼變
    assert len(seen) == len(set(seen)) == 12
    assert total == first["metrics"]["registered_ips"] == sum(range(1, 13))


async def test_audit_count_is_24h_and_admin_only(client, auth_headers, db_session) -> None:
    from sqlalchemy import func, select

    from app.core.audit import append_audit
    from app.models.audit import AuditLog
    s = await _site(db_session)
    for _ in range(3):
        await append_audit(db_session, actor_user_id=None, actor_ip=None, actor_user_agent=None,
                           object_type="subnet", object_id=str(s["child"].id), action="update",
                           diff={"x": 1}, request_id=None)
    # 25 小時前的一筆（直接寫入；稽核表只擋 UPDATE／DELETE）
    db_session.add(AuditLog(ts=datetime.now(UTC) - timedelta(hours=25), object_type="subnet",
                            object_id=s["child"].id, action="update", prev_hash=uuid.uuid4().bytes,
                            this_hash=uuid.uuid4().bytes))
    await db_session.commit()
    d = await _page(client, auth_headers)
    recent = await db_session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.ts >= datetime.now(UTC) - timedelta(hours=24)))
    assert d["metrics"]["audit_24h"] == recent >= 3
    total = await db_session.scalar(select(func.count()).select_from(AuditLog))
    assert d["metrics"]["audit_24h"] == total - 1            # 超過 24 小時的不算

    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=s["small"].id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    mine = await _page(client, _hdr(u))
    assert mine["metrics"]["audit_24h"] is None              # 無權限：不是 0
    assert mine["metrics"]["registered_ips"] == 3            # 只算看得到的子網路
    assert mine["metrics"]["subnets"] == 1
    assert mine["metrics"]["sections"] == 1
    assert [r["count"] for r in mine["status"]["rows"]] == [0, 0, 3]
    assert [i["cidr"] for i in mine["ranking"]["items"]] == ["192.0.2.0/24"]


async def test_display_scope_applies_to_every_number(client, auth_headers, db_session) -> None:
    from app.core.audit import append_audit
    s = await _site(db_session)
    r = await client.put("/api/v1/system/warroom", headers=auth_headers,
                         json={"scope": {"sections": [str(s["sec_b"].id)]}})
    assert r.status_code == 200, r.text
    ip = (await db_session.execute(
        IPAddress.__table__.select().where(IPAddress.subnet_id == s["v6"].id).limit(1))).first()
    await append_audit(db_session, actor_user_id=None, actor_ip=None, actor_user_agent=None,
                       object_type="ip_address", object_id=str(ip.id), action="update", diff=None, request_id=None)
    await append_audit(db_session, actor_user_id=None, actor_ip=None, actor_user_agent=None,
                       object_type="subnet", object_id=str(s["child"].id), action="update", diff=None,
                       request_id=None)                       # 範圍外
    await db_session.commit()
    d = await _page(client, auth_headers)
    m = d["metrics"]
    assert m["registered_ips"] == 12                         # v6 9 ＋ small 3
    assert m["subnets"] == 3                                 # v6、small、empty
    assert m["sections"] == 1
    assert m["audit_24h"] == 1                               # 只算範圍內的子網路與 IP（設定變更本身不算）
    assert d["definitions"]["audit_scoped"] is True
    assert sum(r["count"] for r in d["status"]["rows"]) == 12
    assert d["families"] == {"v4": 3, "v6": 9}


async def test_empty_site_is_empty_not_fake(client, auth_headers) -> None:
    d = await _page(client, auth_headers)
    assert d["metrics"]["registered_ips"] == 0
    assert d["status"]["state"] == "empty"
    assert all(r["pct"] is None for r in d["status"]["rows"])   # 沒有分母就不算百分比
    assert d["ranking"]["state"] == "empty"
