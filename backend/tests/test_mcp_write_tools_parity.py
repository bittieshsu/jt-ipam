"""AI（MCP）工具要跟 REST 走同一套業務規則（2026-10-15 MCP 工具盤點）。

盤點發現幾個工具「回傳不完整」或「繞過 REST 擋下來的規則」，模型拿到之後很有把握地答錯：
- approve_ip_request 只看 is_admin：多關卡審核第一關就直接配發、申請人自己核准自己也行、不能改配發的 IP
- reject_ip_request 沒有理由長度限制
- switch_port_for_ip 沒依交換器可見性遮蔽 FDB、重疊網段默默取第一筆
- list_connection_targets 只查 SSH/RDP/VNC，只開了 BMC／SFTP／noVNC／RustDesk 的位址整列消失
- update_ip 重疊網段任取一筆、主機名稱直接寫欄位（下一輪同步就被蓋回去）
- create_subnet 沒檢查重疊、沒算上層、沒繼承單位
- create_device 類型不驗證（資料庫 CHECK 約束炸成看不懂的錯誤）
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.mcp import tools as T
from app.mcp.tools import IPAMToolError, call_tool
from app.models.address import IPAddress
from app.models.customer import Customer
from app.models.device import Device
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from sqlalchemy import select

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _bust_policy_cache():
    """審核政策有 60 秒的行程內快取；TRUNCATE 清不掉它，前後都清一次免得測試互相污染。"""
    from app.services.ip_request_policy import _bust
    _bust()
    yield
    _bust()


async def _user(db, *, admin: bool = False) -> User:
    from app.core.security import hash_password
    u = User(username=f"u-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local",
             display_name="U", password_hash=hash_password("TestPassword2026!"),
             auth_provider="local", is_active=True, is_admin=admin)
    db.add(u)
    await db.flush()
    return u


async def _section(db) -> Section:
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    return sec


async def _subnet(db, cidr: str, sec: Section | None = None, **kw) -> Subnet:
    sec = sec or await _section(db)
    sub = Subnet(section_id=sec.id, cidr=cidr, **kw)
    db.add(sub)
    await db.flush()
    return sub


async def _audit(db, object_type: str, action: str, object_id=None) -> list:
    from app.models.audit import AuditLog
    stmt = select(AuditLog).where(AuditLog.object_type == object_type, AuditLog.action == action)
    if object_id is not None:
        stmt = stmt.where(AuditLog.object_id == object_id)
    return list((await db.execute(stmt)).scalars().all())


async def _overlap_pair(db, cidr: str = "192.0.2.0/24", ip: str = "192.0.2.9") -> tuple[IPAddress, IPAddress]:
    """兩個單位共用同一段位址：同一個 IP 字串在兩個子網路各一筆（MAC 不同）。"""
    sec = await _section(db)
    out = []
    for i, cust in enumerate(("cust-a", "cust-b")):
        c = Customer(name=f"{cust}-{uuid.uuid4().hex[:4]}")
        db.add(c)
        await db.flush()
        sub = await _subnet(db, cidr, sec, customer_id=c.id)
        a = IPAddress(subnet_id=sub.id, ip=ip, mac=f"02:00:00:00:0{i}:09", state="used")
        db.add(a)
        await db.flush()
        out.append(a)
    await db.commit()
    return out[0], out[1]


# ───────────────────────── IP 申請：核准／駁回 ─────────────────────────


async def _ip_request(db, requester: User, *, cidr: str = "10.20.0.0/24"):
    from app.models.ip_request import IPRequest
    sub = await _subnet(db, cidr)
    r = IPRequest(status="pending", requester_user_id=requester.id, subnet_id=sub.id,
                  purpose="web server", hostname="req-host")
    db.add(r)
    await db.flush()
    await db.commit()
    return r


async def test_approve_follows_stage_policy(db_session, admin_user) -> None:
    """依序多關卡：AI 核准只算「目前這一關」，全部通過才配發（以前第一關就直接配發）。"""
    from app.services.ip_request_policy import set_policy
    a, b, requester = await _user(db_session), await _user(db_session), await _user(db_session)
    await set_policy(db_session, data={"approver_mode": "stages", "stages": [
        {"name": "一審", "user_ids": [str(a.id)], "group_ids": []},
        {"name": "二審", "user_ids": [str(b.id)], "group_ids": []},
    ]}, updated_by_user_id=admin_user.id)
    r = await _ip_request(db_session, requester)

    first = await T.approve_ip_request(db_session, user=admin_user, request_id=str(r.id))
    await db_session.refresh(r)
    assert r.status == "pending" and r.allocated_ip_id is None
    assert first["fulfilled"] is False
    assert [s["approved"] for s in first["stages"]] == [True, False]

    second = await T.approve_ip_request(db_session, user=admin_user, request_id=str(r.id))
    await db_session.refresh(r)
    assert second["fulfilled"] is True and r.status == "fulfilled" and r.allocated_ip_id is not None


async def test_rest_approve_uses_the_same_policy_path(client, auth_headers, db_session, admin_user) -> None:
    """REST 核准改走共用的 approve_per_policy 之後，行為不變：多關卡逐關、全部通過才配發、可改配發 IP。"""
    from app.services.ip_request_policy import set_policy
    a, requester = await _user(db_session), await _user(db_session)
    await set_policy(db_session, data={"approver_mode": "parallel", "stages": [
        {"name": "主管", "user_ids": [str(a.id)], "group_ids": []},
        {"name": "資安", "user_ids": [str(a.id)], "group_ids": []},
    ]}, updated_by_user_id=admin_user.id)
    r = await _ip_request(db_session, requester)
    first = await client.post(f"/api/v1/ip-requests/{r.id}/approve", headers=auth_headers, json={"ip": "10.20.0.88"})
    assert first.status_code == 200 and first.json()["status"] == "pending", first.text
    second = await client.post(f"/api/v1/ip-requests/{r.id}/approve", headers=auth_headers, json={"ip": "10.20.0.88"})
    assert second.status_code == 200 and second.json()["status"] == "fulfilled", second.text
    ipa = await db_session.get(IPAddress, uuid.UUID(second.json()["allocated_ip_id"]))
    assert str(ipa.ip).split("/")[0] == "10.20.0.88"
    third = await client.post(f"/api/v1/ip-requests/{r.id}/approve", headers=auth_headers)
    assert third.status_code == 403, third.text      # 已完成：沒有可核准的關卡


async def test_approve_respects_self_approval_rule(db_session, admin_user) -> None:
    """申請人自己核准自己：政策沒開放就不行，管理員也一樣（REST 的 _can_approve）。"""
    r = await _ip_request(db_session, admin_user)
    with pytest.raises(IPAMToolError, match="self"):
        await T.approve_ip_request(db_session, user=admin_user, request_id=str(r.id))
    with pytest.raises(IPAMToolError, match="self"):
        await T.reject_ip_request(db_session, user=admin_user, request_id=str(r.id), reason="not needed")
    await db_session.refresh(r)
    assert r.status == "pending"


async def test_approve_with_ip_override_and_audit(db_session, admin_user) -> None:
    """審核人可改配發的 IP（畫面上的「將配發的 IP」）；稽核記錄與 REST 相同。"""
    requester = await _user(db_session)
    r = await _ip_request(db_session, requester)
    out = await call_tool(db_session, admin_user, "approve_ip_request",
                          {"request_id": str(r.id), "ip": "10.20.0.77"})
    assert out["fulfilled"] is True and out["allocated_ip"] == "10.20.0.77"
    ipa = await db_session.get(IPAddress, uuid.UUID(out["allocated_ip_id"]))
    assert str(ipa.ip).split("/")[0] == "10.20.0.77"
    assert len(await _audit(db_session, "ip_request", "approve", r.id)) == 1


async def test_approve_override_ip_outside_subnet_is_an_error(db_session, admin_user) -> None:
    requester = await _user(db_session)
    r = await _ip_request(db_session, requester)
    with pytest.raises(IPAMToolError):
        await call_tool(db_session, admin_user, "approve_ip_request",
                        {"request_id": str(r.id), "ip": "10.99.0.1"})


async def test_reject_enforces_reason_length_and_audits(db_session, admin_user) -> None:
    requester = await _user(db_session)
    r = await _ip_request(db_session, requester)
    for bad in ("no", "x" * 1025):
        with pytest.raises(IPAMToolError, match="reason"):
            await T.reject_ip_request(db_session, user=admin_user, request_id=str(r.id), reason=bad)
    out = await T.reject_ip_request(db_session, user=admin_user, request_id=str(r.id), reason="duplicate request")
    assert out["status"] == "rejected"
    assert len(await _audit(db_session, "ip_request", "reject", r.id)) == 1


# ───────────────────────── switch_port_for_ip ─────────────────────────


async def _fdb_site(db):
    from app.models.librenms import FDBEntry, LibreNMSDevice, LibreNMSInstance
    sec = await _section(db)
    sub = await _subnet(db, "198.51.100.0/24", sec)
    sw = Device(name=f"sw-{uuid.uuid4().hex[:4]}", type="switch")
    inst = LibreNMSInstance(name=f"lnms-{uuid.uuid4().hex[:6]}", api_url="https://librenms.example",
                            api_token_enc=b"x", api_token_nonce=b"y")
    db.add_all([sw, inst])
    await db.flush()
    ln = LibreNMSDevice(instance_id=inst.id, legacy_device_id=11, hostname="sw-a", sysname=sw.name,
                        jt_ipam_device_id=sw.id)
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.20", mac="02:00:00:00:00:20", state="used",
                   switch_port=f"{sw.name} / Gi1/0/5", switch_port_confident=True)
    db.add_all([ln, ip])
    await db.flush()
    db.add(FDBEntry(mac="02:00:00:00:00:20", device_id=ln.id, port_name="Gi1/0/5", vlan_id_num=10,
                    source="librenms"))
    await db.commit()
    return sec, sw, ip


async def test_switch_port_hides_switches_the_user_cannot_see(db_session) -> None:
    """與 trace_mac 同一個規則：看得到 IP、看不到那台交換器 → 不給那台交換器的埠。"""
    from app.models.permission import Permission
    sec, sw, _ip = await _fdb_site(db_session)
    u = await _user(db_session)
    db_session.add(Permission(object_type="section", object_id=sec.id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()

    hidden = await T.switch_port_for_ip(db_session, user=u, ip="198.51.100.20")
    assert hidden["locations"] == [] and hidden["likely_access_port"] is None
    # IP 自己記的交換器埠是 IP 的欄位（IP 詳細資料頁本來就看得到）
    assert hidden["switch_port"] == f"{sw.name} / Gi1/0/5" and hidden["switch_port_confident"] is True

    db_session.add(Permission(object_type="device", object_id=sw.id, principal_type="user",
                              principal_id=u.id, level="read"))
    await db_session.commit()
    shown = await T.switch_port_for_ip(db_session, user=u, ip="198.51.100.20")
    assert len(shown["locations"]) == 1
    loc = shown["locations"][0]
    assert (loc["switch"], loc["port"], loc["source"]) == (sw.name, "Gi1/0/5", "librenms")
    assert loc["first_seen_at"] and loc["switch_device_id"] == str(sw.id)


async def test_switch_port_overlapping_ip_is_not_guessed(db_session, admin_user) -> None:
    a, b = await _overlap_pair(db_session)
    with pytest.raises(IPAMToolError) as ei:
        await T.switch_port_for_ip(db_session, user=admin_user, ip="192.0.2.9")
    assert str(a.id) in str(ei.value) and str(b.id) in str(ei.value)
    # 同一個 CIDR 的兩個子網路：給 subnet_cidr 也分不出來，照樣要列候選
    with pytest.raises(IPAMToolError):
        await T.switch_port_for_ip(db_session, user=admin_user, ip="192.0.2.9", subnet_cidr="192.0.2.0/24")
    got = await call_tool(db_session, admin_user, "switch_port_for_ip", {"ip": "192.0.2.9", "ip_id": str(b.id)})
    assert got["mac"] == "02:00:00:00:01:09" and got["ip_id"] == str(b.id)


async def test_rest_switch_port_answers_for_that_address(client, auth_headers, db_session) -> None:
    """REST 的 /addresses/{id}/switch-port 借用這支工具：重疊網段時要回「那一筆」的 MAC，不是任一筆。"""
    a, b = await _overlap_pair(db_session)
    for row in (a, b):
        r = await client.get(f"/api/v1/addresses/{row.id}/switch-port", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["mac"] == str(row.mac), r.json()


# ───────────────────────── list_connection_targets ─────────────────────────


async def test_connection_targets_include_every_console_kind(db_session, admin_user) -> None:
    c = Customer(name=f"cust-{uuid.uuid4().hex[:6]}")
    db_session.add(c)
    await db_session.flush()
    sub = await _subnet(db_session, "203.0.113.0/24", customer_id=c.id)
    rows = {
        "203.0.113.31": {"ssh_enabled": True},
        "203.0.113.32": {"bmc_enabled": True},
        "203.0.113.33": {"sftp_enabled": True},
        "203.0.113.34": {"vnc_enabled": True, "os_guess": "Ubuntu 22.04", "device_kind": "server",
                         "mac": "02:00:00:00:00:34", "effective_status": "online"},
        "203.0.113.35": {},
    }
    for ip, kw in rows.items():
        db_session.add(IPAddress(subnet_id=sub.id, ip=ip, customer_id=c.id, state="used", **kw))
    await db_session.commit()

    out = await T.list_connection_targets(db_session, user=admin_user)
    by_ip = {i["ip"]: i for i in out["items"]}
    assert set(by_ip) == {"203.0.113.31", "203.0.113.32", "203.0.113.33", "203.0.113.34"}
    assert out["total"] == 4 and out["truncated"] is False
    assert by_ip["203.0.113.32"]["bmc"] is True and by_ip["203.0.113.32"]["ssh"] is False
    assert by_ip["203.0.113.33"]["sftp"] is True
    vnc = by_ip["203.0.113.34"]
    assert vnc["customer"] == c.name and vnc["device_kind"] == "server"
    assert vnc["mac"] == "02:00:00:00:00:34" and vnc["status"] == "online"
    assert "Ubuntu" in (vnc["os"] or "")
    assert vnc["subnet"] == "203.0.113.0/24" and vnc["ip_id"]

    page = await T.list_connection_targets(db_session, user=admin_user, limit=2)
    assert page["count"] == 2 and page["total"] == 4 and page["truncated"] is True
    only_bmc = await call_tool(db_session, admin_user, "list_connection_targets", {"protocol": "bmc"})
    assert [i["ip"] for i in only_bmc["items"]] == ["203.0.113.32"]
    only_sftp = await call_tool(db_session, admin_user, "list_connection_targets", {"protocol": "sftp"})
    assert [i["ip"] for i in only_sftp["items"]] == ["203.0.113.33"]


async def test_connection_target_protocol_enum_covers_every_kind() -> None:
    enum = T.TOOLS["list_connection_targets"]["parameters"]["properties"]["protocol"]["enum"]
    assert set(enum) == {"ssh", "sftp", "rdp", "vnc", "novnc", "bmc", "rustdesk"}


# ───────────────────────── update_ip ─────────────────────────


async def test_update_ip_overlap_requires_ip_id(db_session, admin_user) -> None:
    a, b = await _overlap_pair(db_session)
    with pytest.raises(IPAMToolError) as ei:
        await T.update_ip(db_session, user=admin_user, ip="192.0.2.9", note="changed")
    assert str(a.id) in str(ei.value) and str(b.id) in str(ei.value)
    out = await call_tool(db_session, admin_user, "update_ip",
                          {"ip": "192.0.2.9", "ip_id": str(b.id), "note": "changed"})
    assert "note" in out["updated"]
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert (a.note, b.note) == (None, "changed")


async def test_update_ip_ip_id_must_match_ip(db_session, admin_user) -> None:
    a, _b = await _overlap_pair(db_session)
    with pytest.raises(IPAMToolError):
        await T.update_ip(db_session, user=admin_user, ip="192.0.2.10", ip_id=str(a.id), note="x")


async def test_update_ip_hostname_survives_the_next_sync(db_session, admin_user) -> None:
    """主機名稱走人工觀測（與 REST PATCH 相同）：下一輪同步重算有效名稱時不會被蓋回去。"""
    from app.models.ip_hostname import IPHostnameObservation
    from app.services.hostname import apply_observation, recompute_effective
    sub = await _subnet(db_session, "198.51.100.0/24")
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.50", state="used")
    db_session.add(ip)
    await db_session.flush()
    await apply_observation(db_session, ip=ip, source="dns", hostname="old-name")
    await db_session.commit()
    assert ip.hostname == "old-name"

    await T.update_ip(db_session, user=admin_user, ip="198.51.100.50", hostname="new-name")
    await db_session.commit()
    await recompute_effective(db_session, ip=ip)      # 下一輪同步
    await db_session.commit()
    await db_session.refresh(ip)
    assert ip.hostname == "new-name"
    manual = (await db_session.execute(select(IPHostnameObservation).where(
        IPHostnameObservation.ip_id == ip.id, IPHostnameObservation.source == "manual"))).scalars().all()
    assert [m.hostname for m in manual] == ["new-name"]


async def test_update_ip_form_fields_and_audit(db_session, admin_user) -> None:
    c = Customer(name=f"cust-{uuid.uuid4().hex[:6]}")
    dev = Device(name=f"srv-{uuid.uuid4().hex[:4]}", type="server")
    db_session.add_all([c, dev])
    sub = await _subnet(db_session, "198.51.100.0/24")
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.60", state="used")
    db_session.add(ip)
    await db_session.commit()

    out = await call_tool(db_session, admin_user, "update_ip", {
        "ip": "198.51.100.60", "note": "rack 3", "customer": c.name, "device": dev.name,
        "switch_port": "sw-1 / Gi1/0/7", "liveness_from_device": True, "excluded_probes": ["icmp"],
        "ptr_ignore": True, "is_dhcp_server": True, "state": "reserved",
    })
    assert set(out["updated"]) >= {"note", "customer_id", "device_id", "switch_port", "state"}
    await db_session.refresh(ip)
    await db_session.refresh(dev)
    assert (ip.note, ip.customer_id, ip.device_id, ip.switch_port) == ("rack 3", c.id, dev.id, "sw-1 / Gi1/0/7")
    assert ip.liveness_from_device and ip.ptr_ignore and ip.is_dhcp_server and ip.state == "reserved"
    assert ip.excluded_probes == ["icmp"] and ip.exclude_from_ping is True   # 與 REST 一樣雙向同步
    assert dev.primary_ip_id == ip.id                                         # 裝置還沒有主要 IP → 順手補上
    rows = await _audit(db_session, "ip_address", "update", ip.id)
    assert len(rows) == 1 and rows[0].diff["changes"]["note"] == "rack 3"

    # 清空：空字串＝清掉
    await T.update_ip(db_session, user=admin_user, ip="198.51.100.60", customer="", device="", note="")
    await db_session.refresh(ip)
    assert (ip.customer_id, ip.device_id, ip.note) == (None, None, None)


async def test_every_offered_state_can_be_saved(db_session, admin_user) -> None:
    """工具列出的狀態都要存得進去：schema 有 deprecated／quarantine，資料表約束還沒有（存了是 IntegrityError）。"""
    sub = await _subnet(db_session, "198.51.100.0/24")
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.80", state="used"))
    await db_session.commit()
    for name in ("update_ip", "allocate_ip"):
        assert T.TOOLS[name]["parameters"]["properties"]["state"]["enum"]
    for state in T.TOOLS["update_ip"]["parameters"]["properties"]["state"]["enum"]:
        sp = await db_session.begin_nested()
        await call_tool(db_session, admin_user, "update_ip", {"ip": "198.51.100.80", "state": state})
        await db_session.flush()
        await sp.commit()


async def test_update_ip_rejects_bad_values(db_session, admin_user) -> None:
    sub = await _subnet(db_session, "198.51.100.0/24")
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.70", state="used"))
    db_session.add_all([Device(name="dup-dev", type="server"), Device(name="dup-dev", type="server")])
    await db_session.commit()
    for kw in ({"state": "bogus"}, {"customer": "no-such-customer"}, {"device": "dup-dev"},
               {"excluded_probes": ["bogus"]}, {"mac": "zz"}):
        with pytest.raises(IPAMToolError):
            await call_tool(db_session, admin_user, "update_ip", {"ip": "198.51.100.70", **kw})


# ───────────────────────── allocate_ip ─────────────────────────


async def test_allocate_ip_form_fields_and_audit(db_session, admin_user) -> None:
    dev = Device(name=f"srv-{uuid.uuid4().hex[:4]}", type="server")
    db_session.add(dev)
    sub = await _subnet(db_session, "10.60.0.0/24")
    await db_session.commit()
    out = await T.allocate_ip(db_session, user=admin_user, subnet_id=str(sub.id), requested_ip="10.60.0.5",
                              state="reserved", note="for the new NAS", device=dev.name,
                              switch_port="sw-2 / ge-0/0/3")
    ip = await db_session.get(IPAddress, uuid.UUID(out["ip_address_id"]))
    assert (ip.state, ip.note, ip.device_id, ip.switch_port) == ("reserved", "for the new NAS", dev.id,
                                                                 "sw-2 / ge-0/0/3")
    assert len(await _audit(db_session, "ip_address", "create", ip.id)) == 1

    auto = await T.allocate_ip(db_session, user=admin_user, subnet_id=str(sub.id), state="reserved", note="auto")
    ip2 = await db_session.get(IPAddress, uuid.UUID(auto["ip_address_id"]))
    assert (ip2.state, ip2.note) == ("reserved", "auto")
    with pytest.raises(IPAMToolError):
        await T.allocate_ip(db_session, user=admin_user, subnet_id=str(sub.id), state="bogus")


async def test_rest_create_address_keeps_the_customer(client, auth_headers, db_session) -> None:
    """畫面的新增 IP 表單會送單位；REST 建立以前檢查了單位的寫入權、值卻沒寫進去（與 AI 配發共用後一併修正）。"""
    c = Customer(name=f"cust-{uuid.uuid4().hex[:6]}")
    db_session.add(c)
    sub = await _subnet(db_session, "10.62.0.0/24")
    await db_session.commit()
    r = await client.post("/api/v1/addresses", headers=auth_headers,
                          json={"subnet_id": str(sub.id), "ip": "10.62.0.9", "customer_id": str(c.id)})
    assert r.status_code == 201, r.text
    assert r.json()["customer_id"] == str(c.id)


async def test_allocate_ip_does_not_guess_between_overlapping_subnets(db_session, admin_user) -> None:
    sec = await _section(db_session)
    await _subnet(db_session, "192.0.2.0/24", sec)
    await _subnet(db_session, "192.0.2.0/24", sec)
    await db_session.commit()
    with pytest.raises(IPAMToolError, match="overlapping"):
        await T.allocate_ip(db_session, user=admin_user, requested_ip="192.0.2.44")
    n = (await db_session.execute(select(IPAddress).where(IPAddress.ip == "192.0.2.44"))).scalars().all()
    assert n == []


async def test_allocate_ip_honours_the_release_cooldown(db_session, admin_user) -> None:
    """剛釋放的位址還在冷卻期：REST 建立會擋，AI 配發也要擋。"""
    from app.models.ip_cooldown import IPCooldown
    sub = await _subnet(db_session, "10.61.0.0/24")
    now = datetime.now(UTC)
    db_session.add(IPCooldown(subnet_id=sub.id, ip="10.61.0.6", released_at=now,
                              until=now + timedelta(days=1), reason="deleted"))
    await db_session.commit()
    with pytest.raises(IPAMToolError, match="cooldown"):
        await T.allocate_ip(db_session, user=admin_user, subnet_id=str(sub.id), requested_ip="10.61.0.6")


# ───────────────────────── create_subnet ─────────────────────────


async def test_create_subnet_rejects_overlap(db_session, admin_user) -> None:
    sec = await _section(db_session)
    await _subnet(db_session, "10.30.0.0/24", sec)
    await db_session.commit()
    with pytest.raises(IPAMToolError, match="overlap"):
        await T.create_subnet(db_session, user=admin_user, cidr="10.30.0.0/24", section_id=str(sec.id))
    ok = await T.create_subnet(db_session, user=admin_user, cidr="10.30.0.0/24", section_id=str(sec.id),
                               allow_overlap=True)
    assert ok["cidr"] == "10.30.0.0/24"


async def test_create_subnet_nests_and_inherits_customer(db_session, admin_user) -> None:
    c = Customer(name=f"cust-{uuid.uuid4().hex[:6]}")
    db_session.add(c)
    await db_session.flush()
    sec = await _section(db_session)
    parent = await _subnet(db_session, "10.40.0.0/16", sec, customer_id=c.id)
    await db_session.commit()
    out = await T.create_subnet(db_session, user=admin_user, cidr="10.40.1.0/24", section_id=str(sec.id),
                                allow_overlap=True)
    sub = await db_session.get(Subnet, uuid.UUID(out["id"]))
    assert sub.master_subnet_id == parent.id and sub.customer_id == c.id
    assert out["master_subnet"] == "10.40.0.0/16" and out["customer"] == c.name
    assert len(await _audit(db_session, "subnet", "create", sub.id)) == 1


async def test_create_subnet_settings(db_session, admin_user) -> None:
    from app.models.location import Location
    from app.models.vlan import VLAN, VLANDomain
    from app.models.vrf import VRF
    sec = await _section(db_session)
    dom = VLANDomain(name=f"d-{uuid.uuid4().hex[:6]}")
    vrf = VRF(name=f"vrf-{uuid.uuid4().hex[:6]}")
    loc = Location(name=f"loc-{uuid.uuid4().hex[:6]}")
    db_session.add_all([dom, vrf, loc])
    await db_session.flush()
    vlan = VLAN(domain_id=dom.id, number=120, name="users")
    db_session.add(vlan)
    await db_session.commit()
    out = await call_tool(db_session, admin_user, "create_subnet", {
        "cidr": "10.50.0.0/24", "section_name": sec.name, "vlan": "120", "vrf": vrf.name, "location": loc.name,
        "dns_servers": "10.50.0.53", "threshold_pct": 80, "scan_enabled": True, "scan_method": ["icmp"],
        "scan_scope": "known", "is_pool": True, "gateway": "10.50.0.1",
    })
    sub = await db_session.get(Subnet, uuid.UUID(out["id"]))
    assert (sub.vlan_id, sub.vrf_id, sub.location_id) == (vlan.id, vrf.id, loc.id)
    assert (sub.dns_servers, sub.threshold_pct, sub.scan_enabled, sub.scan_scope) == ("10.50.0.53", 80, True, "known")
    assert sub.is_pool is True and str(sub.gateway).split("/")[0] == "10.50.0.1"
    for bad in ({"threshold_pct": 150}, {"gateway": "nope"}, {"scan_scope": "everything"},
                {"scan_method": ["bogus"]}, {"vrf": "no-such-vrf"}):
        with pytest.raises(IPAMToolError):
            await call_tool(db_session, admin_user, "create_subnet",
                            {"cidr": "10.51.0.0/24", "section_id": str(sec.id), **bad})


# ───────────────────────── create_device ─────────────────────────


async def test_create_device_validates_type(db_session, admin_user) -> None:
    from app.schemas.device import _VALID_TYPES
    assert set(T.TOOLS["create_device"]["parameters"]["properties"]["type"]["enum"]) == _VALID_TYPES
    with pytest.raises(IPAMToolError, match="type"):
        await T.create_device(db_session, user=admin_user, name="d-bogus", type="bogus")


async def test_create_device_form_fields_and_audit(db_session, admin_user) -> None:
    from app.models.location import Location, Rack
    c = Customer(name=f"cust-{uuid.uuid4().hex[:6]}")
    loc = Location(name=f"loc-{uuid.uuid4().hex[:6]}")
    db_session.add_all([c, loc])
    await db_session.flush()
    rack = Rack(name=f"r-{uuid.uuid4().hex[:4]}", location_id=loc.id, u_height=42)
    sub = await _subnet(db_session, "198.51.100.0/24")
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.40", state="used")
    db_session.add_all([rack, ip])
    await db_session.commit()

    out = await call_tool(db_session, admin_user, "create_device", {
        "name": "nas-01", "type": "storage", "serial": "SN123", "rack": rack.name, "u_position": 10,
        "u_size": 2, "rack_face": "front", "customer": c.name, "description": "backup NAS",
        "primary_ip": "198.51.100.40",
    })
    dev = await db_session.get(Device, uuid.UUID(out["id"]))
    assert (dev.serial, dev.rack_id, dev.location_id, dev.u_position, dev.u_size, dev.rack_face) == (
        "SN123", rack.id, loc.id, 10, 2, "front")                   # 選了機櫃就不必再給地點
    assert (dev.customer_id, dev.description, dev.primary_ip_id) == (c.id, "backup NAS", ip.id)
    assert dev.type_source == "manual"
    await db_session.refresh(ip)
    assert ip.device_id == dev.id                                    # 主要 IP 指回這台
    assert len(await _audit(db_session, "device", "create", dev.id)) == 1

    # 同一個 U 位已經有裝置 → 與 REST 一樣擋下
    with pytest.raises(IPAMToolError):
        await call_tool(db_session, admin_user, "create_device",
                        {"name": "nas-02", "rack": rack.name, "u_position": 11, "u_size": 1})
    # 機櫃不在指定的地點 → 矛盾要擋
    other = Location(name=f"loc-{uuid.uuid4().hex[:6]}")
    db_session.add(other)
    await db_session.commit()
    with pytest.raises(IPAMToolError):
        await call_tool(db_session, admin_user, "create_device",
                        {"name": "nas-03", "rack": rack.name, "location": other.name, "u_position": 20, "u_size": 1})
