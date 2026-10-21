"""IP 清單／子網路 IP 頁的「批次建立裝置」（使用者 2026-10-20）。

勾選的 IP：已經有裝置的跳過；沒有的 —— 主機名稱（或 MAC、主要 IP）對得到唯一一台既有裝置就關聯過去（不重複建），
否則建一台新的再關聯；同一批裡主機名稱相同的（雙網卡）共用一台。建立裝置是管理員限定（同單筆的「建議裝置」）。
"""
from __future__ import annotations

import uuid

import pytest
from app.models.address import IPAddress
from app.models.audit import AuditLog
from app.models.device import Device
from sqlalchemy import select

from tests.test_ip_conflict_evidence import _subnet

pytestmark = pytest.mark.anyio


async def _seed(db):  # type: ignore[no-untyped-def]
    s1 = await _subnet(db, "198.51.100.0/24")
    s2 = await _subnet(db, "203.0.113.0/24")
    nas = Device(name="nas", type="storage")
    owned = Device(name="owned", type="server")
    db.add_all([nas, owned])
    await db.flush()
    ips = {
        "web": IPAddress(subnet_id=s1.id, ip="198.51.100.10", hostname="web01.corp.example"),
        "linked": IPAddress(subnet_id=s1.id, ip="198.51.100.11", hostname="x", device_id=owned.id),
        "nas": IPAddress(subnet_id=s1.id, ip="198.51.100.12", hostname="nas.corp.example"),
        "db_a": IPAddress(subnet_id=s1.id, ip="198.51.100.13", hostname="db01"),
        "db_b": IPAddress(subnet_id=s2.id, ip="203.0.113.13", hostname="db01"),
        "bare": IPAddress(subnet_id=s1.id, ip="198.51.100.14"),
    }
    db.add_all(ips.values())
    await db.commit()
    return ips, nas, owned


async def test_bulk_create_devices(client, auth_headers, db_session) -> None:
    ips, nas, owned = await _seed(db_session)
    ids = {k: v.id for k, v in ips.items()}
    nas_id, owned_id = nas.id, owned.id
    r = await client.post("/api/v1/addresses/bulk-create-devices", headers=auth_headers,
                          json={"ids": [str(x) for x in ids.values()]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["linked_existing"], body["skipped"]) == (3, 1, 1)
    db_session.expire_all()
    got = {k: (await db_session.get(IPAddress, v)).device_id for k, v in ids.items()}
    assert got["linked"] == owned_id                          # 已經有裝置：不動
    assert got["nas"] == nas_id                               # 對得到既有裝置：關聯過去，不重複建
    assert got["db_a"] == got["db_b"]
    assert got["db_a"] is not None
    names = {d.id: d.name for d in (await db_session.execute(select(Device))).scalars()}
    assert names[got["web"]] == "web01"                       # 名稱取主機名稱第一段
    assert names[got["db_a"]] == "db01"
    assert names[got["bare"]] == "198.51.100.14"              # 沒有主機名稱：用位址
    web = await db_session.get(Device, got["web"])
    assert web.fqdn == "web01.corp.example"
    acts = (await db_session.execute(select(AuditLog.object_type, AuditLog.action).where(
        AuditLog.object_id.in_([str(got["web"]), str(ids["web"])])))).all()
    assert ("device", "create") in acts
    assert ("ip_address", "update") in acts


async def test_bulk_create_devices_is_admin_only(client, db_session) -> None:
    from app.core.security import hash_password
    from app.models.user import User
    from app.services.auth import issue_access_token
    ips, _nas, _owned = await _seed(db_session)
    u = User(username=f"na-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local",
             password_hash=hash_password("TestPassword2026!"), is_admin=False, is_active=True)
    db_session.add(u)
    await db_session.commit()
    r = await client.post("/api/v1/addresses/bulk-create-devices",
                          headers={"Authorization": f"Bearer {issue_access_token(u)}"},
                          json={"ids": [str(ips["web"].id)]})
    assert r.status_code == 403


async def test_too_many_ids(client, auth_headers) -> None:
    r = await client.post("/api/v1/addresses/bulk-create-devices", headers=auth_headers,
                          json={"ids": [str(uuid.uuid4()) for _ in range(2001)]})
    assert r.status_code in (400, 422)
