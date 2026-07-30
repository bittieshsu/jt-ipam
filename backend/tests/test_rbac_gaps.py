"""RBAC 缺口回歸測試（2026-07-30 稽核找到的四類）。

這批全部是「已登入但受限」的帳號本來不該看到卻看得到的資料。共同教訓：
**認證過 ≠ 授權過**，而且 GraphQL 是與 REST 平行的第二個表面，很容易被漏掉。
"""

from __future__ import annotations

import uuid

import pytest
from app.models.location import Location
from app.models.section import Section
from app.models.user import User
from sqlalchemy import func, select


async def _limited_user(db_session) -> User:
    """建一個「已登入但完全沒有任何授權」的帳號（零權限）。"""
    from app.core.security import hash_password
    u = User(
        username=f"limited-{uuid.uuid4().hex[:8]}",
        email=f"limited-{uuid.uuid4().hex[:8]}@test.local",
        display_name="Limited",
        password_hash=hash_password("TestPassword2026!"),
        auth_provider="local",
        is_active=True,
        is_admin=False,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


# ── 1. 地點詳情 / 平面圖的 IDOR ────────────────────────────────────


@pytest.mark.anyio
async def test_location_detail_and_floorplan_deny_limited_user(client, db_session) -> None:
    """`GET /locations/{id}` 與 `/floorplan` 以前只要「已登入」就給 —— 知道 id 就能看。

    這裡走真正的 HTTP 請求（不是內省 dependency 名稱），所以不管將來守門怎麼實作、
    只要受限帳號能讀到就會紅。
    """
    from app.services.auth import issue_access_token

    loc = Location(name=f"rbac-loc-{uuid.uuid4().hex[:6]}")
    db_session.add(loc)
    await db_session.commit()

    user = await _limited_user(db_session)
    headers = {"Authorization": f"Bearer {issue_access_token(user)}"}

    for path in (f"/api/v1/locations/{loc.id}", f"/api/v1/locations/{loc.id}/floorplan"):
        r = await client.get(path, headers=headers)
        # 403（無權）或 404（不洩漏存在性）都可接受；200 不行
        assert r.status_code in (403, 404), f"{path} 回 {r.status_code} → 受限帳號讀得到"


# ── 2. 列表端點的 total 必須跟著可見範圍縮放 ────────────────────────


async def _grant(db_session, user: User, object_type: str, object_id: uuid.UUID) -> None:
    from app.models.permission import Permission
    db_session.add(Permission(
        object_type=object_type, object_id=object_id,
        principal_type="user", principal_id=user.id, level="read",
    ))
    await db_session.commit()


@pytest.mark.anyio
async def test_sections_total_scales_with_visibility(db_session) -> None:
    """`total` 必須是「可見筆數」而不是全系統筆數。

    這裡刻意用**部分可見**（3 個區段只授權 1 個）而不是零權限 —— 零權限會走
    「空 set 直接回 0」的捷徑，測不到 count 查詢有沒有套可見性條件。
    """
    from app.api.v1.endpoints.sections import list_sections

    secs = [Section(name=f"rbac-sec-{uuid.uuid4().hex[:6]}-{i}") for i in range(3)]
    for s in secs:
        db_session.add(s)
    await db_session.commit()
    global_count = int(
        await db_session.scalar(select(func.count()).select_from(Section)) or 0
    )
    assert global_count >= 3

    user = await _limited_user(db_session)
    await _grant(db_session, user, "section", secs[0].id)

    out = await list_sections(user, db_session, page=1, page_size=50)
    assert [i.id for i in out.items] == [secs[0].id]
    assert out.total == 1, (
        f"total={out.total} 但只可見 1 個（全系統 {global_count} 個）→ count 沒套可見性"
    )


@pytest.mark.anyio
async def test_sections_zero_permission_sees_nothing(db_session) -> None:
    from app.api.v1.endpoints.sections import list_sections

    db_session.add(Section(name=f"rbac-sec-{uuid.uuid4().hex[:6]}"))
    await db_session.commit()
    user = await _limited_user(db_session)
    out = await list_sections(user, db_session, page=1, page_size=50)
    assert out.items == []
    assert out.total == 0


@pytest.mark.anyio
async def test_subnets_total_scales_with_visibility(db_session) -> None:
    from app.api.v1.endpoints.subnets import list_subnets
    from app.models.subnet import Subnet

    sec = Section(name=f"rbac-sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    for i in range(3):
        db_session.add(Subnet(cidr=f"10.{200 + i}.0.0/24", section_id=sec.id))
    await db_session.commit()
    global_count = int(
        await db_session.scalar(select(func.count()).select_from(Subnet)) or 0
    )
    assert global_count >= 3

    user = await _limited_user(db_session)
    subs = list((await db_session.execute(
        select(Subnet).where(Subnet.section_id == sec.id).order_by(Subnet.cidr)
    )).scalars().all())
    await _grant(db_session, user, "subnet", subs[0].id)

    out = await list_subnets(
        user, db_session, section_id=None, archived=False, page=1, page_size=50,
    )
    assert [i.id for i in out.items] == [subs[0].id]
    assert out.total == 1, (
        f"total={out.total} 但只可見 1 個（全系統 {global_count} 個）→ count 沒套可見性"
    )


# ── 3. GraphQL 不得成為繞過 REST 限制的旁路 ─────────────────────────


def test_graphql_resolvers_are_all_guarded() -> None:
    """每個 resolver 都要有可見性過濾或全域讀取檢查（`me` 例外：只回自己）。

    釘住這件事，因為 GraphQL 與 REST 是兩個平行表面 —— 新增 resolver 時
    很容易只想到 REST 那邊的守門。
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "app" / "graphql" / "schema.py"
    text = src.read_text(encoding="utf-8")
    unguarded = []
    for block in re.split(r"(?=@strawberry\.field)", text)[1:]:
        m = re.search(r"async def (\w+)\(", block)
        if not m:
            continue
        name = m.group(1)
        if name == "me":                       # 只回自己，不需過濾
            continue
        if not any(
            k in block
            for k in ("filter_visible", "visible_ids", "get_object_permission",
                      "_assert_global_read")
        ):
            unguarded.append(name)
    assert not unguarded, f"這些 GraphQL resolver 沒有任何權限過濾：{unguarded}"


@pytest.mark.anyio
async def test_graphql_global_read_matches_rest(db_session) -> None:
    """GraphQL 的全域讀取判斷要與 REST 的 `require_global_read` 一致。"""
    from app.graphql.schema import _assert_global_read

    user = await _limited_user(db_session)
    with pytest.raises(PermissionError):
        await _assert_global_read(db_session, user)


@pytest.mark.anyio
async def test_graphql_devices_filtered_for_limited_user(db_session) -> None:
    """零權限帳號用 GraphQL 列裝置要拿到空清單，不是全部裝置。"""
    from app.graphql.schema import Query
    from app.models.device import Device

    db_session.add(Device(name=f"rbac-dev-{uuid.uuid4().hex[:6]}", type="server"))
    await db_session.commit()

    user = await _limited_user(db_session)

    class _Info:
        context = {"session": db_session, "user": user}

    got = await Query().devices(_Info(), type=None, limit=200)  # type: ignore[arg-type]
    assert got == [], f"零權限帳號看到了 {len(got)} 台裝置"


# ── 4. 防火牆唯讀檢視：pfSense 與 FortiGate 的守門要一致 ─────────────


@pytest.mark.anyio
async def test_firewall_view_guards_are_consistent() -> None:
    """唯讀檢視頁需要的端點必須是 global_read；異動與「即時連設備」必須是 admin。

    背景：pfSense 的規則／別名原本是 admin-only，但前端「防火牆 (pfSense)」檢視頁
    掛在「進階」（非管理區），所以具全域讀取的非 admin 看得到選單卻撞 403。
    FortiGate 也有同樣問題 —— 檢視頁要用 `GET /fortigate` 列舉防火牆，那支卻在
    admin router 上。兩邊都已對齊，這個測試釘住它。
    """
    from app.main import create_app

    def guards(dep, acc=None, depth=0):
        acc = acc if acc is not None else set()
        if depth > 8:
            return acc
        c = getattr(dep, "call", None)
        if c is not None:
            acc.add(getattr(c, "__name__", ""))
        for sub in getattr(dep, "dependencies", []) or []:
            guards(sub, acc, depth + 1)
        return acc

    found: dict[tuple[str, str], set[str]] = {}
    for r in create_app().routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None)
        dnt = getattr(r, "dependant", None)
        if not methods or dnt is None or "/lookup/" in path:
            continue
        if not (path.startswith("/api/v1/pfsense") or path.startswith("/api/v1/fortigate")):
            continue
        g = guards(dnt)
        for m in methods - {"HEAD", "OPTIONS"}:
            found[(m, path)] = g

    # 檢視頁要用的 → 必須是 global_read（不能是 admin-only）
    for key in (
        ("GET", "/api/v1/pfsense"),
        ("GET", "/api/v1/pfsense/{fw_id}/rules"),
        ("GET", "/api/v1/pfsense/{fw_id}/aliases"),
        ("GET", "/api/v1/fortigate"),
        ("GET", "/api/v1/fortigate/{fw_id}/policies"),
        ("GET", "/api/v1/fortigate/{fw_id}/addresses"),
    ):
        assert key in found, f"{key} 路由不存在，測試需更新"
        assert "require_global_read" in found[key], f"{key} 不是 global_read → 檢視頁會撞 403"
        assert "require_admin" not in found[key], f"{key} 仍掛 admin → 檢視頁會撞 403"

    # 異動與即時連設備 → 必須是 admin
    for key in (
        ("POST", "/api/v1/pfsense"),
        ("DELETE", "/api/v1/pfsense/{fw_id}"),
        ("POST", "/api/v1/pfsense/{fw_id}/sync"),
        ("GET", "/api/v1/pfsense/{fw_id}/nat"),      # 即時連到設備抓資料
        ("POST", "/api/v1/fortigate"),
        ("DELETE", "/api/v1/fortigate/{fw_id}"),
        ("POST", "/api/v1/fortigate/{fw_id}/sync"),
    ):
        assert key in found, f"{key} 路由不存在，測試需更新"
        assert "require_admin" in found[key], f"{key} 缺少 require_admin"


def test_firewall_read_schemas_expose_no_secrets() -> None:
    """實例清單開放給 global_read 的前提：回應不含 API token / 金鑰。"""
    from app.api.v1.endpoints.pfsense import PfSenseRead
    from app.schemas.fortigate import FortiGateRead

    for schema in (FortiGateRead, PfSenseRead):
        for name in schema.model_fields:
            low = name.lower()
            if low == "has_key":          # 只是「有沒有設」的布林旗標
                continue
            assert not any(
                k in low for k in ("token", "password", "secret", "key", "enc", "nonce")
            ), f"{schema.__name__}.{name} 看起來會洩漏機密，不該開放給 global_read"


# ── 5. 客戶彙整靠的是「向下繼承」，不是漏過濾 ──────────────────────


def test_customer_is_ancestor_of_its_resources() -> None:
    """釘住 customer → section/subnet/ip/device 的向下繼承。

    `/customers/{id}/summary` 只檢查對該客戶的 read，然後回傳旗下全部資源 ——
    這是**正確的**，前提是這條繼承關係成立。若哪天有人改動繼承表卻沒同步該端點，
    這個測試會先紅，提醒去補逐物件過濾。
    """
    from app.services.permission import _ANCESTOR_TYPES

    for child in ("section", "subnet", "ip", "device"):
        assert "customer" in _ANCESTOR_TYPES[child], (
            f"customer 不再是 {child} 的上層 → /customers/{{id}}/summary 必須改成逐物件過濾"
        )
