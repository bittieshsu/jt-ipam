"""寫入時引用其他物件的權限守門（2026-10-07 全站 RBAC 稽核）。

- #2 掃描代理、主控台出口、跳板只有管理員能指派（REST 上「代理負責哪些子網路」本來就是管理員限定，
  但子網路／IP 的編輯可以繞過去；跳板與代理會照這個設定替人開主控台、掃描別的網段）
- #3 變更會影響權限繼承的上層（子網路的區段／單位、區段的單位、IP 的單位）：對物件本身要 admin、
  對目的地要寫入權；只影響顯示階層的上層（上層子網路、上層區段）要看得到
- #4 IP 不可以掛到看不到的裝置；裝置建議不可以回看不到的裝置
- #7 關掉異常偵測或 AI 巡檢只有管理員可以（集中設定本來就是管理員限定）
"""
from __future__ import annotations

import uuid
from typing import Any

from app.models.address import IPAddress
from app.models.customer import Customer
from app.models.device import Device
from app.models.permission import Permission
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User


async def _user(db) -> User:  # type: ignore[no-untyped-def]
    from app.core.security import hash_password
    u = User(username=f"u-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local", display_name="U",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True)
    db.add(u)
    await db.commit()
    return u


def _hdr(user: User) -> dict[str, str]:
    from app.services.auth import issue_access_token
    return {"Authorization": f"Bearer {issue_access_token(user)}"}


async def _grant(db, user: User, otype: str, oid: Any, level: str) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select
    row = (await db.execute(select(Permission).where(
        Permission.object_type == otype, Permission.object_id == oid,
        Permission.principal_type == "user", Permission.principal_id == user.id))).scalar_one_or_none()
    if row is None:
        db.add(Permission(object_type=otype, object_id=oid, principal_type="user", principal_id=user.id, level=level))
    else:
        row.level = level
    await db.commit()


async def _base(db):  # type: ignore[no-untyped-def]
    a = Section(name=f"a-{uuid.uuid4().hex[:6]}")
    b = Section(name=f"b-{uuid.uuid4().hex[:6]}")
    c = Customer(name=f"c-{uuid.uuid4().hex[:6]}")
    db.add_all([a, b, c])
    await db.flush()
    sub = Subnet(section_id=a.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.10", state="active")
    db.add(ip)
    await db.commit()
    return a, b, c, sub, ip


def _code(r) -> str:  # type: ignore[no-untyped-def]
    d = r.json().get("detail")
    return d.get("code", "") if isinstance(d, dict) else str(d)


async def test_only_admins_assign_agents_and_jump_hosts(client, db_session) -> None:
    a, _b, _c, sub, ip = await _base(db_session)
    u = await _user(db_session)
    await _grant(db_session, u, "section", a.id, "write")
    h = _hdr(u)
    rid = str(uuid.uuid4())
    for field in ("scan_agent_id", "console_agent_id", "jump_host_id"):
        r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=h, json={field: rid})
        assert r.status_code == 403 and _code(r) == "infra_assignment_admin_only", (field, r.text)
    r = await client.patch(f"/api/v1/addresses/{ip.id}", headers=h, json={"jump_host_id": rid})
    assert r.status_code == 403 and _code(r) == "infra_assignment_admin_only", r.text
    # 表單每次都會送全部欄位：值沒變就照常存
    r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=h,
                           json={"description": "x", "scan_agent_id": None, "jump_host_id": None})
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/subnets", headers=h,
                          json={"section_id": str(a.id), "cidr": "203.0.113.0/24", "jump_host_id": rid})
    assert r.status_code == 403 and _code(r) == "infra_assignment_admin_only", r.text
    r = await client.post("/api/v1/subnets", headers=h, json={"section_id": str(a.id), "cidr": "203.0.113.0/24"})
    assert r.status_code == 201, r.text


async def test_only_admins_turn_off_anomaly_or_ai_audit(client, db_session) -> None:
    a, _b, _c, sub, _ip = await _base(db_session)
    u = await _user(db_session)
    await _grant(db_session, u, "section", a.id, "write")
    for field in ("anomaly_enabled", "ai_audit_enabled"):
        r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=_hdr(u), json={field: False})
        assert r.status_code == 403 and _code(r) == "subnet_monitoring_admin_only", (field, r.text)
    r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=_hdr(u),
                           json={"anomaly_enabled": True, "ai_audit_enabled": True, "description": "y"})
    assert r.status_code == 200, r.text


async def test_moving_across_permission_boundaries(client, db_session) -> None:
    a, b, c, sub, ip = await _base(db_session)
    u = await _user(db_session)
    await _grant(db_session, u, "subnet", sub.id, "write")
    await _grant(db_session, u, "customer", c.id, "admin")
    h = _hdr(u)
    # 只有寫入權：不可以把子網路搬到別的單位或區段（搬到自己是管理員的單位底下＝提權）
    r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=h, json={"customer_id": str(c.id)})
    assert r.status_code == 403 and _code(r) == "move_needs_admin", r.text
    r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=h, json={"section_id": str(b.id)})
    assert r.status_code == 403 and _code(r) == "move_needs_admin", r.text
    r = await client.patch(f"/api/v1/addresses/{ip.id}", headers=h, json={"customer_id": str(c.id)})
    assert r.status_code == 403 and _code(r) == "move_needs_admin", r.text
    # 對子網路有 admin，但對目的地區段沒有寫入權 → 仍然不行；給了寫入權才可以
    await _grant(db_session, u, "subnet", sub.id, "admin")
    r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=h, json={"section_id": str(b.id)})
    assert r.status_code == 403 and _code(r) == "move_needs_admin", r.text
    await _grant(db_session, u, "section", b.id, "write")
    r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=h, json={"section_id": str(b.id)})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=h, json={"customer_id": str(c.id)})
    assert r.status_code == 200, r.text
    # 上層子網路只影響顯示：看得到就可以，看不到回 404
    hidden = Subnet(section_id=a.id, cidr="198.51.0.0/16")
    db_session.add(hidden)
    await db_session.commit()
    r = await client.patch(f"/api/v1/subnets/{sub.id}", headers=h, json={"master_subnet_id": str(hidden.id)})
    assert r.status_code == 404, r.text


async def test_section_customer_move_needs_admin(client, db_session) -> None:
    a, _b, c, _sub, _ip = await _base(db_session)
    u = await _user(db_session)
    await _grant(db_session, u, "section", a.id, "write")
    await _grant(db_session, u, "customer", c.id, "admin")
    r = await client.patch(f"/api/v1/sections/{a.id}", headers=_hdr(u), json={"customer_id": str(c.id)})
    assert r.status_code == 403 and _code(r) == "move_needs_admin", r.text
    r = await client.patch(f"/api/v1/sections/{a.id}", headers=_hdr(u), json={"description": "ok"})
    assert r.status_code == 200, r.text


async def test_ip_cannot_point_at_an_invisible_device(client, db_session) -> None:
    a, _b, _c, sub, ip = await _base(db_session)
    dev = Device(name="laptop-07", type="workstation")
    db_session.add(dev)
    await db_session.commit()
    u = await _user(db_session)
    await _grant(db_session, u, "section", a.id, "write")
    h = _hdr(u)
    r = await client.patch(f"/api/v1/addresses/{ip.id}", headers=h, json={"device_id": str(dev.id)})
    assert r.status_code == 404, r.text
    r = await client.post("/api/v1/addresses", headers=h,
                          json={"subnet_id": str(sub.id), "ip": "198.51.100.11", "device_id": str(dev.id)})
    assert r.status_code == 404, r.text
    r = await client.post(f"/api/v1/addresses/{ip.id}/device-suggestion/apply", headers=h,
                          json={"device_id": str(dev.id)})
    assert r.status_code == 404, r.text
    # 裝置建議：主機名稱對得上，但這台裝置看不到 → 不建議
    ip.hostname = "laptop-07"
    await db_session.commit()
    s = (await client.get(f"/api/v1/addresses/{ip.id}/device-suggestion", headers=h)).json()
    assert s["existing_device_id"] is None, s
    # 看得到但不能寫：可以掛，但不會順手改那台裝置的主要 IP
    await _grant(db_session, u, "device", dev.id, "read")
    r = await client.patch(f"/api/v1/addresses/{ip.id}", headers=h, json={"device_id": str(dev.id)})
    assert r.status_code == 200, r.text
    await db_session.refresh(dev)
    assert dev.primary_ip_id is None


async def test_phpipam_delete_matches_rest(client, db_session) -> None:
    """phpIPAM 相容層刪 IP 以前只要子網路寫入權（REST 要 admin），也跳過冷卻期與異動記錄。"""
    from app.api.phpipam.helpers import phpipam_current_user
    from app.models.ip_change_log import IPChangeLog
    from sqlalchemy import select
    a, _b, _c, sub, ip = await _base(db_session)
    ip_id, sub_id = ip.id, sub.id
    u = await _user(db_session)
    await _grant(db_session, u, "section", a.id, "write")
    app = client._transport.app   # conftest 每個測試 create_app() 一份
    app.dependency_overrides[phpipam_current_user] = lambda: u
    try:
        r = await client.delete(f"/api/phpipam/app/addresses/{ip_id}/")
        assert r.status_code == 404, r.text
        await _grant(db_session, u, "section", a.id, "admin")
        r = await client.delete(f"/api/phpipam/app/addresses/{ip_id}/")
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.pop(phpipam_current_user, None)
    db_session.expire_all()
    assert await db_session.get(IPAddress, ip_id) is None
    logs = (await db_session.execute(select(IPChangeLog).where(IPChangeLog.ip_text.like("198.51.100.10%")))).scalars().all()
    assert any(x.event_type == "deleted" for x in logs)
    from app.services import ip_lifecycle
    assert await ip_lifecycle.cooldown_for(db_session, subnet_id=sub_id, ip="198.51.100.10") is not None


async def test_relation_chains_hide_objects_you_cannot_see(client, db_session) -> None:
    """IP 與裝置的關係鏈以前串出看不到的裝置、機櫃、地點、子網路、區段名稱（2026-10-07 稽核 #9）。"""
    from app.models.location import Location, Rack
    a, _b, _c, sub, ip = await _base(db_session)
    loc = Location(name="hidden-room")
    db_session.add(loc)
    await db_session.flush()
    rk = Rack(name="hidden-rack", location_id=loc.id, u_height=42)
    db_session.add(rk)
    await db_session.flush()
    dev = Device(name="hidden-server", type="server", rack_id=rk.id, primary_ip_id=ip.id)
    db_session.add(dev)
    await db_session.flush()
    ip.device_id = dev.id
    await db_session.commit()
    # 只看得到子網路：IP 的鏈不可以出現裝置、機櫃、地點
    u = await _user(db_session)
    await _grant(db_session, u, "subnet", sub.id, "read")
    chain = (await client.get(f"/api/v1/addresses/{ip.id}/relations", headers=_hdr(u))).json()["chain"]
    types = {n["type"] for n in chain}
    assert "ip" in types and not types & {"device", "rack", "location"}, chain
    assert "section" not in types   # 區段本身沒有授權
    # 只看得到裝置：裝置的鏈不可以出現它的 IP、子網路、區段、機櫃、地點
    v = await _user(db_session)
    await _grant(db_session, v, "device", dev.id, "read")
    chain = (await client.get(f"/api/v1/devices/{dev.id}/relations", headers=_hdr(v))).json()["chain"]
    types = {n["type"] for n in chain}
    assert "device" in types and not types & {"ip", "subnet", "section", "rack", "location"}, chain


async def test_lists_do_not_name_hidden_neighbours(client, db_session) -> None:
    """IP 清單的裝置名稱、裝置清單的對應 IP id 也要看得到才給。"""
    a, _b, _c, sub, ip = await _base(db_session)
    dev = Device(name="hidden-server", type="server", primary_ip_id=ip.id)
    db_session.add(dev)
    await db_session.flush()
    ip.device_id = dev.id
    other_dev = Device(name="visible-box", type="server")
    db_session.add(other_dev)
    await db_session.commit()
    u = await _user(db_session)
    await _grant(db_session, u, "subnet", sub.id, "read")
    items = (await client.get(f"/api/v1/addresses?subnet_id={sub.id}", headers=_hdr(u))).json()["items"]
    row = next(x for x in items if str(x["ip"]).startswith("198.51.100.10"))
    assert row.get("device_name") in (None, ""), row
    v = await _user(db_session)
    await _grant(db_session, v, "device", dev.id, "read")
    items = (await client.get("/api/v1/devices", headers=_hdr(v))).json()["items"]
    d = next(x for x in items if x["id"] == str(dev.id))
    assert d.get("ip_address_id") is None and d.get("ip_match_id") is None, d


async def test_stale_reminder_needs_write_and_counts_only_this_subnet(client, db_session) -> None:
    """唯讀帳號以前可以對所有管理員與外部通知管道發提醒，筆數與天數也照單全收。"""
    a, _b, _c, sub, ip = await _base(db_session)
    u = await _user(db_session)
    await _grant(db_session, u, "subnet", sub.id, "read")
    body = {"subnet_id": str(sub.id), "ids": [str(ip.id)], "days": 30}
    r = await client.post("/api/v1/addresses/notify-stale", headers=_hdr(u), json=body)
    assert r.status_code == 404, r.text
    await _grant(db_session, u, "subnet", sub.id, "write")
    fake = {**body, "ids": [str(ip.id)] + [str(uuid.uuid4()) for _ in range(5)]}
    r = await client.post("/api/v1/addresses/notify-stale", headers=_hdr(u), json=fake)
    assert r.status_code == 200 and r.json()["ip_count"] == 1, r.text
    r = await client.post("/api/v1/addresses/notify-stale", headers=_hdr(u), json={**body, "days": 10**9})
    assert r.status_code == 422, r.text


async def test_mcp_subnet_lookup_does_not_tell_hidden_from_missing(db_session) -> None:
    from app.mcp.tools import IPAMToolError, get_subnet_usage
    a, _b, _c, sub, _ip = await _base(db_session)
    u = await _user(db_session)
    msgs = []
    for sid in (str(sub.id), str(uuid.uuid4())):
        try:
            await get_subnet_usage(db_session, user=u, subnet_id=sid)
        except IPAMToolError as exc:
            msgs.append(str(exc))
    assert len(msgs) == 2 and msgs[0] == msgs[1], msgs
