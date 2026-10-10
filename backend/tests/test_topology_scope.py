"""拓樸圖逐物件權限（2026-10-09 合規核對：以前只有「全開或全關」）。

只被授權部分物件的帳號以前整張圖 403；現在看得到自己範圍內的裝置與子網路、兩端都在範圍內的
連線。VPN 與虛擬機是全域基礎設施資料，這種帳號一律不畫並在回應裡寫明。零權限帳號仍然 403。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.address import IPAddress
from app.models.device import Device
from app.models.permission import Permission
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from app.models.physical import VPNTunnel
from app.services.auth import issue_access_token


async def _world(db_session):
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    mine = Subnet(section_id=sec.id, cidr="192.0.2.0/24")
    theirs = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add_all([mine, theirs])
    await db_session.flush()
    d_mine = Device(name="sw-mine", type="switch")
    d_theirs = Device(name="sw-theirs", type="switch")
    db_session.add_all([d_mine, d_theirs])
    await db_session.flush()
    db_session.add_all([
        IPAddress(subnet_id=mine.id, ip="192.0.2.1", state="active", device_id=d_mine.id),
        IPAddress(subnet_id=theirs.id, ip="198.51.100.1", state="active", device_id=d_theirs.id),
        VPNTunnel(name="t1", type="wireguard", a_device_id=d_mine.id, b_device_id=d_theirs.id,
                  a_endpoint="203.0.113.1", b_endpoint="203.0.113.2"),
    ])
    u = User(username=f"p-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@example.invalid",
             password_hash="x", is_active=True, is_admin=False)
    db_session.add(u)
    await db_session.flush()
    db_session.add_all([
        Permission(object_type="subnet", object_id=mine.id, principal_type="user", principal_id=u.id, level="read"),
        Permission(object_type="device", object_id=d_mine.id, principal_type="user", principal_id=u.id, level="read"),
    ])
    await db_session.commit()
    return u, d_mine, d_theirs, mine, theirs


@pytest.mark.anyio
async def test_partial_scope_sees_only_its_part(db_session, client) -> None:
    u, d_mine, d_theirs, mine, theirs = await _world(db_session)
    r = await client.get("/api/v1/topology", headers={"Authorization": f"Bearer {issue_access_token(u)}"})
    assert r.status_code == 200, "只被授權部分物件的帳號以前整張圖 403"
    g = r.json()
    ids = {n["data"]["id"] for n in g["nodes"]}
    assert str(d_mine.id) in ids and str(d_theirs.id) not in ids
    assert f"subnet:{mine.id}" in ids and f"subnet:{theirs.id}" not in ids
    assert not any(i.startswith("vpnsite:") for i in ids), "VPN 是全域資料，部分範圍帳號不畫"
    assert g["scope"] == "limited" and "vpn" in g["hidden_layers"]
    for e in g["edges"]:
        assert e["data"]["source"] in ids and e["data"]["target"] in ids


@pytest.mark.anyio
async def test_zero_visibility_is_still_refused(db_session, client) -> None:
    u = User(username="z-user", email="z@example.invalid", password_hash="x", is_active=True)
    db_session.add(u)
    await db_session.commit()
    r = await client.get("/api/v1/topology", headers={"Authorization": f"Bearer {issue_access_token(u)}"})
    assert r.status_code == 403


@pytest.mark.anyio
async def test_admin_gets_the_full_graph(db_session, client, auth_headers) -> None:
    await _world(db_session)
    g = (await client.get("/api/v1/topology", headers=auth_headers)).json()
    assert "scope" not in g
    assert any(n["data"]["id"].startswith("vpnsite:") or n["data"]["label"] == "sw-theirs" for n in g["nodes"])


@pytest.mark.anyio
async def test_mcp_tool_matches_rest(db_session) -> None:
    from app.mcp.tools import authorize_tool, get_topology
    u, *_ = await _world(db_session)
    assert await authorize_tool(db_session, u, "get_topology") is None
    out = await get_topology(db_session, user=u)
    assert out["scope"].startswith("limited")
    assert all("sw-theirs" not in (e["from"], e["to"]) for e in out["edges"])
