"""AI 對話／MCP：ISOinsight 來源租約的唯讀工具（權限、範圍、總數、語意）。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.mcp.tools import TOOLS, IPAMToolError, authorize_tool
from app.models.isoinsight import IsoInsightLease, IsoInsightSource
from app.models.permission import Permission
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User


async def _setup(db):
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    mine = Subnet(section_id=sec.id, cidr="192.0.2.0/24")
    other = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add_all([mine, other])
    await db.flush()
    src = IsoInsightSource(name="iso-main", base_url="https://192.0.2.1", username="u", password_enc=b"x",
                           password_nonce=b"x", scope_subnet_ids=[mine.id, other.id])
    db.add(src)
    await db.flush()
    now = datetime.now(UTC)
    rows = [("192.0.2.20", "02005e005320", mine, now + timedelta(hours=2)),
            ("192.0.2.21", "02005e005321", mine, now - timedelta(hours=1)),
            ("198.51.100.20", "02005e005330", other, now + timedelta(hours=2)),
            ("203.0.113.5", "02005e005340", None, now + timedelta(hours=2))]
    for ip, mk, sub, end in rows:
        db.add(IsoInsightLease(source_id=src.id, ip=ip, mac_key=mk, mac=":".join(mk[i:i + 2] for i in range(0, 12, 2)),
                               name=f"host-{ip.rsplit('.', 1)[1]}", subnet_id=sub.id if sub else None,
                               match_status="matched" if sub else "no_subnet",
                               start_at=now - timedelta(hours=3), end_at=end,
                               first_observed_at=now, lease_observed_at=now))
    await db.commit()
    return mine, other, src


async def _user(db, subnet=None) -> User:
    from app.core.security import hash_password
    u = User(username=f"na-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local", display_name="NA",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True, is_admin=False)
    db.add(u)
    await db.flush()
    if subnet is not None:
        db.add(Permission(object_type="subnet", object_id=subnet.id, principal_type="user", principal_id=u.id,
                          level="read"))
    await db.commit()
    return u


fn = TOOLS["list_isoinsight_leases"]["fn"]


async def test_admin_sees_everything_with_a_total(db_session, admin_user) -> None:
    await _setup(db_session)
    out = await fn(db_session, user=admin_user, limit=2)
    assert out["scope"] == "all" and out["count"] == 4 and out["returned"] == 2
    assert "NOT whether the device is online" in out["note"]
    assert out["sources"][0]["name"] == "iso-main"


async def test_visibility_is_pushed_into_the_query(db_session) -> None:
    mine, other, _src = await _setup(db_session)
    u = await _user(db_session, mine)
    assert await authorize_tool(db_session, u, "list_isoinsight_leases") is None
    out = await fn(db_session, user=u)
    assert out["count"] == 2 and {r["ip"] for r in out["leases"]} == {"192.0.2.20", "192.0.2.21"}
    with pytest.raises(IPAMToolError):
        await fn(db_session, user=u, subnet_cidr="198.51.100.0/24")


async def test_zero_visibility_is_denied(db_session) -> None:
    await _setup(db_session)
    u = await _user(db_session)
    assert await authorize_tool(db_session, u, "list_isoinsight_leases") is not None


async def test_filters_scope_state_and_mac(db_session, admin_user) -> None:
    await _setup(db_session)
    out = await fn(db_session, user=admin_user, subnet_cidr="192.0.2.0/24", state="active")
    assert out["scope"] == "192.0.2.0/24" and [r["ip"] for r in out["leases"]] == ["192.0.2.20"]
    assert out["leases"][0]["lease_state"] == "active" and out["leases"][0]["source"] == "iso-main"
    assert out["leases"][0]["observed_at"]
    out = await fn(db_session, user=admin_user, mac="02-00-5E-00-53-30")
    assert [r["ip"] for r in out["leases"]] == ["198.51.100.20"]
    out = await fn(db_session, user=admin_user, source="nope")
    assert out["count"] == 0
    with pytest.raises(IPAMToolError):
        await fn(db_session, user=admin_user, state="online")


async def test_empty_result_is_empty_not_invented(db_session, admin_user) -> None:
    out = await fn(db_session, user=admin_user, ip="192.0.2.99")
    assert out["count"] == 0 and out["leases"] == []
