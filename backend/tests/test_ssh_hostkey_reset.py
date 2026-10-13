"""SSH 主機金鑰的重設路徑（使用者 2026-10-12，殘留資料全面檢查）。

位址換給另一台設備之後，記住的主機金鑰一定對不上，主控台只會一直說「可能遭中間人攻擊」。
金鑰刻意不在換手時自動清掉（自動清就等於攻擊者換掉 MAC 就能讓人重新信任），所以要有人工重設：
- 只有管理員或對子網路有寫入權限的人可以清（只有連線管理權限的人能連線，但不能決定要不要重新信任）
- 看不到這個位址的人拿到 404，跟位址不存在一樣
- 清掉寫一筆稽核，帶舊金鑰的指紋；清掉後下次連線會再跳出新金鑰讓人確認
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = pytest.mark.anyio

_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"


async def _user(db_session, *, can_ssh: bool = False):
    from app.core.security import hash_password
    from app.models.user import User
    u = User(username=f"hk-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@e.test",
             password_hash=hash_password("Xx!12345678xX"), is_admin=False, is_active=True,
             can_ssh=can_ssh)
    db_session.add(u)
    await db_session.flush()
    return u


async def _ip(db_session):
    from app.models.address import IPAddress
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    ipa = IPAddress(subnet_id=sub.id, ip="198.51.100.7", ssh_enabled=True, ssh_host_key=_KEY)
    db_session.add(ipa)
    await db_session.flush()
    return ipa


def _grant(db_session, ipa, u, level):
    from app.models.permission import Permission
    db_session.add(Permission(object_type="subnet", object_id=ipa.subnet_id,
                              principal_type="user", principal_id=u.id, level=level))


def _hdr(u):
    from app.services.auth import issue_access_token
    return {"Authorization": f"Bearer {issue_access_token(u)}"}


async def _key_of(session_factory, ipa):
    from app.models.address import IPAddress
    async with session_factory() as s:
        return (await s.get(IPAddress, ipa.id)).ssh_host_key


async def test_admin_clears_the_pinned_key_and_it_is_audited(
        client: AsyncClient, db_session, auth_headers, session_factory) -> None:
    from app.models.audit import AuditLog
    ipa = await _ip(db_session)
    await db_session.commit()
    r = await client.delete(f"/api/v1/addresses/{ipa.id}/ssh/host-key", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"cleared": True}
    assert await _key_of(session_factory, ipa) is None
    async with session_factory() as s:
        row = (await s.execute(select(AuditLog).where(
            AuditLog.object_id == ipa.id, AuditLog.action == "ssh.hostkey_reset"))).scalar_one()
    assert row.diff["fingerprint"].startswith("SHA256:"), "稽核要留舊金鑰的指紋，事後才查得出清掉的是哪一把"
    # 下一張票證說「沒有記住的金鑰」→ 前端會再跳出指紋確認
    t = await client.post(f"/api/v1/addresses/{ipa.id}/ssh/ticket", headers=auth_headers)
    if t.status_code == 200:
        assert t.json()["host_key_pinned"] is False


async def test_nothing_pinned_is_not_an_error(client: AsyncClient, db_session, auth_headers) -> None:
    from app.models.audit import AuditLog
    ipa = await _ip(db_session)
    ipa.ssh_host_key = None
    await db_session.commit()
    r = await client.delete(f"/api/v1/addresses/{ipa.id}/ssh/host-key", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"cleared": False}
    n = (await db_session.execute(select(AuditLog).where(
        AuditLog.object_id == ipa.id, AuditLog.action == "ssh.hostkey_reset"))).all()
    assert n == [], "沒有東西被清掉就不寫稽核"


async def test_subnet_write_can_clear(client: AsyncClient, db_session, session_factory) -> None:
    u = await _user(db_session)
    ipa = await _ip(db_session)
    _grant(db_session, ipa, u, "write")
    await db_session.commit()
    r = await client.delete(f"/api/v1/addresses/{ipa.id}/ssh/host-key", headers=_hdr(u))
    assert r.status_code == 200, r.text
    assert await _key_of(session_factory, ipa) is None


async def test_console_capability_alone_cannot_clear(client: AsyncClient, db_session, session_factory) -> None:
    """能連線不等於能決定重新信任：只有 can_ssh＋唯讀的人清不掉。"""
    u = await _user(db_session, can_ssh=True)
    ipa = await _ip(db_session)
    _grant(db_session, ipa, u, "read")
    await db_session.commit()
    r = await client.delete(f"/api/v1/addresses/{ipa.id}/ssh/host-key", headers=_hdr(u))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "console_hostkey_reset_forbidden"
    assert await _key_of(session_factory, ipa) == _KEY


async def test_invisible_address_looks_missing(client: AsyncClient, db_session, session_factory) -> None:
    u = await _user(db_session, can_ssh=True)
    ipa = await _ip(db_session)
    await db_session.commit()
    r = await client.delete(f"/api/v1/addresses/{ipa.id}/ssh/host-key", headers=_hdr(u))
    missing = await client.delete(f"/api/v1/addresses/{uuid.uuid4()}/ssh/host-key", headers=_hdr(u))
    assert r.status_code == missing.status_code == 404
    assert await _key_of(session_factory, ipa) == _KEY
