"""權限指派清單的「權限」欄（2026-10-22 使用者）：每個帳號一眼看出掛了哪些角色、有幾筆直接的物件授權。"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.permission import Permission
from app.models.user import Group, User, UserGroupMember

pytestmark = pytest.mark.anyio


async def test_summary_lists_roles_and_direct_grant_counts(client, auth_headers, db_session) -> None:
    from app.core.security import hash_password
    from app.services.permission import seed_default_roles
    await seed_default_roles(db_session)
    u = User(username=f"tv-{uuid.uuid4().hex[:6]}", email="", password_hash=hash_password("TestPassword2026!"),
             auth_provider="local", is_active=True)
    db_session.add(u)
    await db_session.flush()
    ro = (await db_session.execute(select(Group).where(Group.name == "唯讀檢視者"))).scalar_one()
    db_session.add(UserGroupMember(user_id=u.id, group_id=ro.id))
    for _ in range(2):
        db_session.add(Permission(object_type="subnet", object_id=uuid.uuid4(), principal_type="user",
                                  principal_id=u.id, level="read"))
    await db_session.commit()
    r = await client.get("/api/v1/system/permissions/summary", headers=auth_headers)
    assert r.status_code == 200, r.text
    mine = r.json()[str(u.id)]
    assert mine["roles"] == ["唯讀檢視者"]
    assert mine["grants"] == 2


async def test_summary_is_admin_only(client, db_session) -> None:
    from app.core.security import hash_password
    from app.services.auth import issue_access_token
    u = User(username=f"x-{uuid.uuid4().hex[:6]}", email="", password_hash=hash_password("TestPassword2026!"),
             auth_provider="local", is_active=True)
    db_session.add(u)
    await db_session.commit()
    r = await client.get("/api/v1/system/permissions/summary",
                         headers={"Authorization": f"Bearer {issue_access_token(u)}"})
    assert r.status_code == 403
