"""MCP 工具不可以比對應的 REST 寬（2026-10-07 全站 RBAC 稽核 #8）。

- get_device 以前回這台裝置的所有 IP、主機名稱、MAC，不看子網路可見性
- list_ip_requests 以前用「全域讀取」判斷能不能看全部，REST 是「審核人」
"""
from __future__ import annotations

import json
import uuid

from app.mcp.server import process_message
from app.models.address import IPAddress
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


async def _call(user, name, args):  # type: ignore[no-untyped-def]
    r = await process_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}}, user)
    res = r.get("result") or {}
    text = (res.get("content") or [{}])[0].get("text", "{}")
    try:
        return json.loads(text)
    except ValueError:
        return {"_text": text}


async def test_get_device_lists_only_visible_ips(db_session) -> None:
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    mine = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    other = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    dev = Device(name="laptop-07", type="workstation")
    db_session.add_all([mine, other, dev])
    await db_session.flush()
    db_session.add_all([IPAddress(subnet_id=mine.id, ip="198.51.100.7", device_id=dev.id, hostname="laptop-07"),
                        IPAddress(subnet_id=other.id, ip="203.0.113.7", device_id=dev.id, hostname="hidden-nic")])
    await db_session.commit()
    u = await _user(db_session)
    db_session.add_all([
        Permission(object_type="device", object_id=dev.id, principal_type="user", principal_id=u.id, level="read"),
        Permission(object_type="subnet", object_id=mine.id, principal_type="user", principal_id=u.id, level="read")])
    await db_session.commit()
    body = await _call(u, "get_device", {"device_id": str(dev.id)})
    blob = json.dumps(body, ensure_ascii=False, default=str)
    assert "198.51.100.7" in blob, body
    assert "203.0.113.7" not in blob and "hidden-nic" not in blob, body


async def test_ip_requests_full_list_only_for_approvers(db_session) -> None:
    from app.models.ip_request import IPRequest
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    requester = await _user(db_session)
    reader = await _user(db_session)
    db_session.add(IPRequest(subnet_id=sub.id, requester_user_id=requester.id, status="pending",
                             hostname="laptop-07", purpose="test"))
    # 全域讀取（萬用的 subnet read）但不是審核人：只能看自己的申請
    db_session.add(Permission(object_type="subnet", object_id=None, principal_type="user", principal_id=reader.id,
                              level="read"))
    await db_session.commit()
    body = await _call(reader, "list_ip_requests", {})
    assert body.get("count") == 0, body


async def test_ip_history_arp_stays_inside_the_subnet(db_session) -> None:
    """重疊網段：受限帳號查 IP 歷史，不可以看到別的單位同一個位址的 ARP。"""
    from datetime import UTC, datetime
    from app.models.librenms import ARPEntry
    from app.models.vrf import VRF
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    v1, v2 = VRF(name=f"v1-{uuid.uuid4().hex[:6]}"), VRF(name=f"v2-{uuid.uuid4().hex[:6]}")
    db_session.add_all([sec, v1, v2])
    await db_session.flush()
    mine = Subnet(section_id=sec.id, cidr="198.51.100.0/24", vrf_id=v1.id)
    theirs = Subnet(section_id=sec.id, cidr="198.51.100.0/24", vrf_id=v2.id)
    db_session.add_all([mine, theirs])
    await db_session.flush()
    db_session.add(IPAddress(subnet_id=mine.id, ip="198.51.100.7"))
    now = datetime.now(UTC)
    db_session.add_all([
        ARPEntry(ip="198.51.100.7", mac="00:00:5e:00:53:01", subnet_id=mine.id, source="opnsense",
                 first_seen_at=now, last_seen_at=now),
        ARPEntry(ip="198.51.100.7", mac="00:00:5e:00:53:02", subnet_id=theirs.id, source="opnsense",
                 first_seen_at=now, last_seen_at=now),
        ARPEntry(ip="198.51.100.7", mac="00:00:5e:00:53:03", subnet_id=None, source="librenms",
                 first_seen_at=now, last_seen_at=now)])
    await db_session.commit()
    u = await _user(db_session)
    db_session.add(Permission(object_type="subnet", object_id=mine.id, principal_type="user", principal_id=u.id,
                              level="read"))
    await db_session.commit()
    body = await _call(u, "get_ip_history", {"ip": "198.51.100.7"})
    macs = {e.get("mac") for e in body.get("events", []) if e.get("kind") == "arp"}
    assert macs == {"00:00:5e:00:53:01"}, body
