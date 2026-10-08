"""語意搜尋要照權限過濾（2026-10-07 RBAC 稽核 #1）。

以前 GET /ai/semantic-search 完全沒帶使用者：任何登入帳號（含零權限帳號、自建的 API token）都拿得到
全站子網路 CIDR 與說明、IP 與主機名稱、裝置名稱與說明，每類最多 100 筆，連歸檔的也在內。
"""
from __future__ import annotations

import uuid

from app.models.address import IPAddress
from app.models.permission import Permission
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from app.services import ai as ai_service
from sqlalchemy import text

_VEC = [0.01] * 768


def _hdr(user: User) -> dict[str, str]:
    from app.services.auth import issue_access_token
    return {"Authorization": f"Bearer {issue_access_token(user)}"}


async def test_semantic_search_only_returns_what_the_caller_can_see(client, db_session, monkeypatch) -> None:
    from app.core.security import hash_password

    async def fake_embed(session, text_in):  # type: ignore[no-untyped-def]
        return _VEC
    monkeypatch.setattr(ai_service, "embed", fake_embed)
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    mine = Subnet(section_id=sec.id, cidr="198.51.100.0/24", description="mine")
    other = Subnet(section_id=sec.id, cidr="203.0.113.0/24", description="other")
    db_session.add_all([mine, other])
    await db_session.flush()
    db_session.add_all([IPAddress(subnet_id=mine.id, ip="198.51.100.5", hostname="laptop-07", description="x"),
                        IPAddress(subnet_id=other.id, ip="203.0.113.5", hostname="hidden-host", description="x")])
    u = User(username=f"u-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", display_name="U",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True)
    db_session.add(u)
    await db_session.flush()
    db_session.add(Permission(object_type="subnet", object_id=mine.id, principal_type="user", principal_id=u.id,
                              level="read"))
    await db_session.commit()
    lit = "[" + ",".join("0.010000" for _ in _VEC) + "]"
    for table in ("subnets", "ip_addresses"):
        await db_session.execute(text(f"UPDATE {table} SET description_embedding = CAST(:v AS vector)"), {"v": lit})  # noqa: S608
    await db_session.commit()

    body = (await client.get("/api/v1/ai/semantic-search", params={"q": "anything"}, headers=_hdr(u))).json()
    labels = {r["label"] for k in ("subnets", "ip_addresses", "devices") for r in body.get(k, [])}
    assert "198.51.100.0/24" in labels and "198.51.100.5" in labels
    assert "203.0.113.0/24" not in labels and "203.0.113.5" not in labels, labels

    nobody = User(username=f"n-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.local", display_name="N",
                  password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True)
    db_session.add(nobody)
    await db_session.commit()
    body = (await client.get("/api/v1/ai/semantic-search", params={"q": "anything"}, headers=_hdr(nobody))).json()
    assert not any(body.get(k) for k in ("subnets", "ip_addresses", "devices")), body
