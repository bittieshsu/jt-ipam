"""全站搜尋的設定（使用者 2026-10-13）：隨打即找、搜哪些類型與欄位、結果的排列順序。

- 任何登入的人讀得到（搜尋框要知道要不要隨打即找、怎麼排）；只有管理員能改，改了留稽核
- 存不進去的情況要講原因：一個類型都沒開、開了的類型一個欄位都沒勾
- 搜尋照設定：關掉的類型不出現、沒勾的欄位不比對、回應帶排列順序
- 權限不變；AI 工具（不帶設定）照樣全搜
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.services import search_settings as S

pytestmark = pytest.mark.anyio


def test_normalize_drops_unknowns_and_appends_new_types() -> None:
    got = S.normalize({"instant": False, "types": [
        {"type": "device", "enabled": True, "fields": ["description", "bogus", "name"]},
        {"type": "nope", "enabled": True, "fields": ["x"]},
        {"type": "device", "enabled": False, "fields": []},          # 重複的只留第一個
    ]})
    assert got["instant"] is False
    assert got["types"][0] == {"type": "device", "enabled": True, "fields": ["name", "description"]}
    assert [x["type"] for x in got["types"]][1:] == [t for t in S.CATALOG if t != "device"]
    assert S.normalize(None) == S.defaults()


def test_validation_explains_what_is_missing() -> None:
    from app.core.ui_error import UiError
    none_on = S.normalize({"types": [{"type": t, "enabled": False, "fields": list(f)} for t, f in S.CATALOG.items()]})
    with pytest.raises(UiError) as ei:
        S.validate(none_on)
    assert ei.value.code == "search_settings_no_type"
    no_field = S.normalize({"types": [{"type": "rack", "enabled": True, "fields": []}]})
    with pytest.raises(UiError) as ei:
        S.validate(no_field)
    assert ei.value.code == "search_settings_no_field"
    assert "rack" in ei.value.params["types"]


async def _user(db_session, *, admin=False):  # type: ignore[no-untyped-def]
    from app.core.security import hash_password
    from app.models.user import User
    from app.services.auth import issue_access_token
    u = User(username=f"u-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@e.test",
             password_hash=hash_password("Xx!12345678xX"), is_admin=admin, is_active=True)
    db_session.add(u)
    await db_session.commit()
    return {"Authorization": f"Bearer {issue_access_token(u)}"}


async def test_anyone_signed_in_can_read_only_admins_can_change(client: AsyncClient, auth_headers,
                                                                db_session) -> None:
    from app.models.audit import AuditLog
    h = await _user(db_session)
    r = await client.get("/api/v1/system/search-settings", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["instant"] is True
    assert r.json()["catalog"]["ip_address"] == list(S.CATALOG["ip_address"])
    body = {"instant": False, "types": [{"type": "device", "enabled": True, "fields": ["name"]}]}
    assert (await client.put("/api/v1/system/search-settings", headers=h, json=body)).status_code == 403
    r = await client.put("/api/v1/system/search-settings", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["instant"] is False
    assert r.json()["types"][0] == {"type": "device", "enabled": True, "fields": ["name"]}
    again = await client.get("/api/v1/system/search-settings", headers=h)
    assert again.json()["types"][0]["type"] == "device"
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.action == "update"))).scalars().all()
    assert any((x.diff or {}).get("target") == "global_search" for x in rows)


async def test_saving_nothing_searchable_is_refused_with_a_reason(client: AsyncClient, auth_headers) -> None:
    body = {"instant": True, "types": [{"type": t, "enabled": False, "fields": list(f)}
                                       for t, f in S.CATALOG.items()]}
    r = await client.put("/api/v1/system/search-settings", headers=auth_headers, json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "search_settings_no_type"


async def _seed(db_session):  # type: ignore[no-untyped-def]
    from app.models.address import IPAddress
    from app.models.device import Device
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.5", hostname="zeta-web"))
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.6", hostname="db01", note="zeta backup target"))
    db_session.add(Device(name="zeta-switch", type="switch"))
    await db_session.commit()


async def test_search_follows_the_settings(client: AsyncClient, auth_headers, db_session) -> None:
    await _seed(db_session)
    r = await client.get("/api/v1/search", params={"q": "zeta"}, headers=auth_headers)
    labels = {h["label"] for h in r.json()["results"]}
    assert {"zeta-web", "db01", "zeta-switch"} <= labels
    # 關掉裝置、IP 不比對備註；IP 排到最前面
    await client.put("/api/v1/system/search-settings", headers=auth_headers, json={"instant": True, "types": [
        {"type": "ip_address", "enabled": True, "fields": ["ip", "hostname"]},
        {"type": "device", "enabled": False, "fields": ["name"]}]})
    r = await client.get("/api/v1/search", params={"q": "zeta"}, headers=auth_headers)
    body = r.json()
    labels = {h["label"] for h in body["results"]}
    assert "zeta-web" in labels
    assert "db01" not in labels, "沒勾「備註」就不比對備註"
    assert "zeta-switch" not in labels, "關掉的類型不出現"
    assert body["order"][0] == "ip_address"
    assert "device" not in body["order"]


async def test_ai_search_tool_is_not_limited_by_the_search_box_settings(db_session, admin_user) -> None:
    """設定只管畫面上方的搜尋框；AI 工具不帶設定，照樣全搜。"""
    from app.services.search import search
    await _seed(db_session)
    await S.set_search_settings(db_session, {"types": [{"type": "rack", "enabled": True, "fields": ["name"]}]})
    await db_session.commit()
    got = await search(db_session, user=admin_user, q="zeta")
    assert "zeta-switch" in {h["label"] for h in got["results"]}
