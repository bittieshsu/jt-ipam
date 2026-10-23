"""儀表板「IP 使用率」卡片改成最多 4 個自選子網路（客戶 2026-10-23）。

客戶原話：網段一多（12 個）單一總表的使用率沒有任何用途，只有在單一網段下才有意義。
- 選哪幾個跟著帳號存：user_preferences.pinned["usage_subnets"]（跟「IP 可用性追蹤」同一套），上限 4
- 存的時候就把看不到的、不存在的、已歸檔的拿掉（受限帳號釘不了範圍外的子網路），讀的時候再擋一次
  （權限之後被收回的情況）
- 沒選（或選的全部看不到了）→ 自動：可見子網路裡使用率最高的 4 個
- 數字跟子網路清單頁同一個定義（services.subnet.get_usage）
"""
from __future__ import annotations

import uuid

import pytest
from app.models.address import IPAddress
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User, UserPreference

pytestmark = pytest.mark.anyio

URL = "/api/v1/dashboard/subnet-usage"


async def _fill(db, sub: Subnet, base: str, n: int) -> None:  # type: ignore[no-untyped-def]
    """在 sub 裡建 n 筆 IP：base 是前三段（IPv4）或前綴（IPv6，以 :: 結尾）。"""
    for i in range(1, n + 1):
        ip = f"{base}{i:x}" if base.endswith("::") else f"{base}.{i}"
        db.add(IPAddress(subnet_id=sub.id, ip=ip, state="active"))


async def _site(db):  # type: ignore[no-untyped-def]
    """六個使用率各不相同的子網路＋一個已歸檔的滿載子網路＋一個 IPv6。"""
    sec = Section(name=f"su-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    s = {
        "full": Subnet(section_id=sec.id, cidr="10.1.0.0/30", description="Point-to-point"),  # 2/2 = 100%
        "half": Subnet(section_id=sec.id, cidr="10.2.0.0/29", description="Lab"),             # 3/6 = 50%
        "mid": Subnet(section_id=sec.id, cidr="10.3.0.0/28", description="Office"),           # 6/14 = 42.86%
        "low": Subnet(section_id=sec.id, cidr="10.4.0.0/24", description="Servers"),          # 1/254
        "huge": Subnet(section_id=sec.id, cidr="10.0.0.0/8", description="Supernet"),         # 5/16777214
        "empty": Subnet(section_id=sec.id, cidr="10.5.0.0/27", description="Empty"),          # 0/30
        "v6": Subnet(section_id=sec.id, cidr="2001:db8:1::/64", description="IPv6 LAN"),
        "archived": Subnet(section_id=sec.id, cidr="10.6.0.0/30", description="Old"),
    }
    db.add_all(s.values())
    await db.flush()
    await _fill(db, s["full"], "10.1.0", 2)
    await _fill(db, s["half"], "10.2.0", 3)
    await _fill(db, s["mid"], "10.3.0", 6)
    await _fill(db, s["low"], "10.4.0", 1)
    await _fill(db, s["huge"], "10.9.9", 5)
    await _fill(db, s["v6"], "2001:db8:1::", 4)
    await _fill(db, s["archived"], "10.6.0", 2)
    from datetime import UTC, datetime
    s["archived"].archived_at = datetime.now(UTC)
    await db.commit()
    return s


async def _limited_user(db) -> User:  # type: ignore[no-untyped-def]
    from app.core.security import hash_password
    u = User(
        username=f"su-{uuid.uuid4().hex[:8]}", email=f"su-{uuid.uuid4().hex[:8]}@test.local",
        display_name="Limited", password_hash=hash_password("TestPassword2026!"),
        auth_provider="local", is_active=True, is_admin=False,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _grant(db, user: User, subnet: Subnet) -> None:  # type: ignore[no-untyped-def]
    from app.models.permission import Permission
    db.add(Permission(object_type="subnet", object_id=subnet.id,
                      principal_type="user", principal_id=user.id, level="read"))
    await db.commit()


def _headers(user: User) -> dict[str, str]:
    from app.services.auth import issue_access_token
    return {"Authorization": f"Bearer {issue_access_token(user)}"}


async def _pin(client, headers, ids, **other) -> dict:  # type: ignore[no-untyped-def]
    r = await client.patch("/api/v1/me/preferences",
                           json={"pinned": {"usage_subnets": [str(x) for x in ids], **other}}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_default_is_top_four_by_utilization(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    r = await client.get(URL, headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "auto"
    assert body["max"] == 4
    # 已歸檔的（100%）不算；/8 照真實比例排（5/16777214 比 /24 的 1/254 還低）
    assert [i["cidr"] for i in body["items"]] == ["10.1.0.0/30", "10.2.0.0/29", "10.3.0.0/28", "10.4.0.0/24"]
    first = body["items"][0]
    assert first["subnet_id"] == str(s["full"].id)
    assert first["description"] == "Point-to-point"
    assert first["ip_version"] == 4


async def test_numbers_match_the_subnet_page(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    await _pin(client, auth_headers, [s["mid"].id, s["huge"].id, s["v6"].id, s["empty"].id])
    body = (await client.get(URL, headers=auth_headers)).json()
    assert body["mode"] == "pinned"
    for item in body["items"]:
        u = (await client.get(f"/api/v1/subnets/{item['subnet_id']}/usage", headers=auth_headers)).json()
        assert (item["used"], item["total"], item["used_pct"]) == (u["used"], u["total"], u["used_pct"])
    v6 = next(i for i in body["items"] if i["cidr"] == "2001:db8:1::/64")
    assert v6["ip_version"] == 6
    assert v6["used"] == 4


async def test_pinned_keeps_the_users_order(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    await _pin(client, auth_headers, [s["empty"].id, s["low"].id])
    body = (await client.get(URL, headers=auth_headers)).json()
    assert body["mode"] == "pinned"
    assert [i["subnet_id"] for i in body["items"]] == [str(s["empty"].id), str(s["low"].id)]


async def test_at_most_four_are_stored_and_junk_is_dropped(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    ids = [s["low"].id, s["low"].id, uuid.uuid4(), s["archived"].id,
           s["full"].id, s["half"].id, s["mid"].id, s["empty"].id]
    pref = await _pin(client, auth_headers, ids, uptime_ips=["keep-me"])
    # 重複、不存在、已歸檔的拿掉；只留前 4 個
    assert pref["pinned"]["usage_subnets"] == [str(s["low"].id), str(s["full"].id), str(s["half"].id),
                                               str(s["mid"].id)]
    # 同一包裡別的命名空間不受影響
    assert pref["pinned"]["uptime_ips"] == ["keep-me"]
    r = await client.patch("/api/v1/me/preferences",
                           json={"pinned": {"usage_subnets": ["not-a-uuid", str(s["low"].id)]}},
                           headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["pinned"]["usage_subnets"] == [str(s["low"].id)]


async def test_clearing_goes_back_to_automatic(client, auth_headers, db_session) -> None:
    s = await _site(db_session)
    await _pin(client, auth_headers, [s["empty"].id])
    assert (await client.get(URL, headers=auth_headers)).json()["mode"] == "pinned"
    await _pin(client, auth_headers, [])
    body = (await client.get(URL, headers=auth_headers)).json()
    assert body["mode"] == "auto"
    assert len(body["items"]) == 4


async def test_limited_user_cannot_pin_or_see_outside_scope(client, db_session) -> None:
    s = await _site(db_session)
    user = await _limited_user(db_session)
    await _grant(db_session, user, s["low"])
    await _grant(db_session, user, s["empty"])
    h = _headers(user)

    # 自動：只在自己看得到的裡面挑（滿載的 10.1.0.0/30 不能出現）
    body = (await client.get(URL, headers=h)).json()
    assert body["mode"] == "auto"
    assert [i["cidr"] for i in body["items"]] == ["10.4.0.0/24", "10.5.0.0/27"]

    # 釘範圍外的 → 存的時候就被拿掉
    pref = await _pin(client, h, [s["full"].id, s["low"].id])
    assert pref["pinned"]["usage_subnets"] == [str(s["low"].id)]
    body = (await client.get(URL, headers=h)).json()
    assert [i["subnet_id"] for i in body["items"]] == [str(s["low"].id)]

    # 權限之後被收回、資料庫裡還留著舊的 id → 讀的時候也要擋
    row = await db_session.get(UserPreference, user.id)
    row.pinned = {"usage_subnets": [str(s["full"].id), str(s["half"].id)]}
    await db_session.commit()
    body = (await client.get(URL, headers=h)).json()
    assert body["mode"] == "auto"   # 選的全部看不到了 → 回到自動
    assert str(s["full"].id) not in {i["subnet_id"] for i in body["items"]}
    assert str(s["half"].id) not in {i["subnet_id"] for i in body["items"]}


async def test_user_without_any_scope_gets_nothing(client, db_session) -> None:
    await _site(db_session)
    user = await _limited_user(db_session)
    body = (await client.get(URL, headers=_headers(user))).json()
    assert body == {"mode": "auto", "max": 4, "items": []}


async def test_subnet_list_server_side_search(client, auth_headers, db_session) -> None:
    """挑選器要在伺服器端搜尋（子網路很多的站台不能整包載入）。"""
    s = await _site(db_session)

    async def q(term: str, headers=auth_headers) -> list[str]:  # type: ignore[no-untyped-def]
        r = await client.get("/api/v1/subnets", params={"q": term, "page_size": 50}, headers=headers)
        assert r.status_code == 200, r.text
        return sorted(i["cidr"] for i in r.json()["items"])

    assert await q("10.3.") == ["10.3.0.0/28"]
    assert await q("office") == ["10.3.0.0/28"]               # 描述，不分大小寫
    assert await q("10.4.0.77") == ["10.0.0.0/8", "10.4.0.0/24"]  # 位址 → 包含它的子網路
    assert await q("2001:db8:1") == ["2001:db8:1::/64"]
    assert await q("100%") == []                               # LIKE 萬用字元要跳脫
    # 受限帳號搜不到範圍外的
    user = await _limited_user(db_session)
    await _grant(db_session, user, s["low"])
    assert await q("10.", headers=_headers(user)) == ["10.4.0.0/24"]
