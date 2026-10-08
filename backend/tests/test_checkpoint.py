"""Check Point 整合（第一階段：Management API，唯讀）。使用者 2026-10-08：比照 Palo Alto／FortiGate 支援 Check Point，R81.20。

跟 PA／FG 不同：設定在管理伺服器（SMS／Multi-Domain／Smart-1 Cloud），一台管很多閘道與政策套件；
整合單位是「管理伺服器」。測試對象是照官方 API 文件做的模擬伺服器（tests/checkpoint_mock.py），實機驗收待 VM。
"""
from __future__ import annotations

import json
import uuid

import pytest
from app.core.config import get_settings
from app.models.address import IPAddress
from app.models.checkpoint import (
    CheckPointGateway,
    CheckPointObject,
    CheckPointRule,
    CheckPointServer,
)
from app.models.nat import NATTranslation
from app.models.section import Section
from app.models.subnet import Subnet
from app.services import checkpoint as cp
from sqlalchemy import select

from tests.checkpoint_mock import API_KEY, NETWORK_LAYER, CheckPointMock


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setenv("OUTBOUND_ALLOW_CIDRS", "127.0.0.1/32")
    get_settings.cache_clear()
    s = CheckPointMock()
    yield s
    s.close()
    get_settings.cache_clear()


async def _server(db, srv, **kw) -> CheckPointServer:  # type: ignore[no-untyped-def]
    inst = CheckPointServer(name=f"cp-{uuid.uuid4().hex[:6]}", api_url=srv.url, verify_tls=False,
                            sync_policies=True, sync_nat=True, sync_objects=True, **kw)
    db.add(inst)
    await db.flush()
    inst.secret_enc, inst.secret_nonce = cp.encrypt_secret_for(inst.id, API_KEY)
    await db.commit()
    return inst


async def _net(db) -> dict[str, IPAddress]:  # type: ignore[no-untyped-def]
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    out = {}
    for last in (10, 20):
        ip = IPAddress(subnet_id=sub.id, ip=f"198.51.100.{last}", state="active")
        db.add(ip)
        out[str(last)] = ip
    await db.commit()
    return out


async def test_login_is_read_only_with_the_api_key_and_always_logs_out(db_session, srv) -> None:
    inst = await _server(db_session, srv)
    async with cp.Session(inst) as s:
        assert s.version == "1.9"
        await s.call("show-api-versions")
    login = srv.hits("POST", "/web_api/login")[0]
    body = json.loads(login["body"])
    assert body["api-key"] == API_KEY and body["read-only"] is True
    # 實機（R81.20）：唯讀登入帶 session-name／session-description 會被拒絕（400），整個整合就登不進去
    assert not {"session-name", "session-description", "session-comments"} & set(body)
    assert srv.logged_out == ["sid-1"]
    # 中途出錯也要登出（不然會佔滿管理伺服器的連線數）
    srv.fail["show-hosts"] = (500, {"code": "generic_error", "message": "boom"})
    with pytest.raises(cp.CheckPointError):
        async with cp.Session(inst) as s:
            await s.call("show-hosts")
    assert srv.logged_out == ["sid-1", "sid-2"]


async def test_wrong_key_is_a_clear_error_without_the_key(db_session, srv) -> None:
    inst = await _server(db_session, srv)
    inst.secret_enc, inst.secret_nonce = cp.encrypt_secret_for(inst.id, "wrong-key")
    with pytest.raises(cp.CheckPointError) as ei:
        async with cp.Session(inst):
            pass
    assert ei.value.code == "cp_login_failed"
    assert "wrong-key" not in str(ei.value) and API_KEY not in str(ei.value)


async def test_sync_mirrors_gateways_objects_rules_and_nat(db_session, srv) -> None:
    ips = await _net(db_session)
    inst = await _server(db_session, srv)
    summary = await cp.sync_instance(db_session, inst)
    await db_session.commit()
    assert "errors" not in summary, summary
    assert summary["gateways"] == 2 and summary["rules"] == 6 and summary["objects"] == 7

    gws = {g.name: g for g in (await db_session.execute(select(CheckPointGateway))).scalars().all()}
    assert str(gws["cp-gw-01"].ipv4_address) == "192.0.2.1"

    objs = {o.name: o for o in (await db_session.execute(select(CheckPointObject).where(
        CheckPointObject.server_id == inst.id))).scalars().all()}
    assert objs["web-01"].value == "198.51.100.10" and objs["web-01"].obj_type == "host"
    assert objs["net-lan"].value == "198.51.100.0/24"
    assert objs["range-dhcp"].value == "198.51.100.100-198.51.100.150"
    assert objs["grp-servers"].members == ["web-01", "db-01"]
    assert objs["lan-but-dhcp"].obj_type == "group-with-exclusion"

    rules = {r.uid: r for r in (await db_session.execute(select(CheckPointRule).where(
        CheckPointRule.server_id == inst.id))).scalars().all()}
    # 分頁（模擬伺服器每頁 2 筆）、段落攤平、內嵌層也抓
    assert set(rules) == {"rl-1", "rl-2", "rl-3", "rl-4", "rl-5", "rl-a1"}
    r1 = rules["rl-1"]
    assert (r1.package, r1.layer, r1.section, r1.rule_number) == ("Standard", "Network", "Web", 1)
    assert r1.source == "any" and r1.destination == "web-01" and r1.action == "Accept" and r1.service == "https"
    assert r1.hits == 7 and r1.last_hit_at is not None
    assert rules["rl-3"].source_negate is True
    assert rules["rl-5"].enabled is False
    assert rules["rl-a1"].layer == "Network › lay-apps" and rules["rl-a1"].source == "lan-but-dhcp"

    nats = (await db_session.execute(select(NATTranslation).where(
        NATTranslation.source_origin == f"checkpoint:{inst.id}"))).scalars().all()
    # 只收目的地轉換（對外開放）；hide NAT 與自動靜態 NAT 的出向那條都不收
    by_ext = {x.external_id.rsplit(":", 1)[-1]: x for x in nats}
    assert set(by_ext) == {"nat-1", "nat-a2"}
    n = by_ext["nat-1"]
    assert n.type == "port_forward" and n.dst_ip_id == ips["10"].id and "203.0.113.10" in (n.description or "")
    assert "static" in (n.description or "")
    # 主機的自動靜態 NAT：規則兩邊都是同一台主機，對外位址在主機的 nat-settings（實機才發現）
    a = by_ext["nat-a2"]
    assert a.dst_ip_id == ips["10"].id
    assert "203.0.113.11" in (a.description or "") and "198.51.100.10" in (a.description or "")
    assert (a.description or "").index("203.0.113.11") < (a.description or "").index("198.51.100.10")

    await db_session.refresh(inst)
    assert inst.last_error is None and inst.api_version == "1.9"


async def test_server_side_removals_are_mirrored_and_a_failed_section_keeps_its_data(db_session, srv, monkeypatch) -> None:
    await _net(db_session)
    inst = await _server(db_session, srv)
    await cp.sync_instance(db_session, inst)
    await db_session.commit()
    import tests.checkpoint_mock as m
    monkeypatch.setattr(m, "NETWORK_LAYER", [x for x in NETWORK_LAYER if x.get("uid") != "rl-5"])
    srv.fail["show-hosts"] = (500, {"code": "generic_error", "message": "boom"})
    summary = await cp.sync_instance(db_session, inst)
    await db_session.commit()
    assert "objects" in summary["errors"]
    uids = set((await db_session.execute(select(CheckPointRule.uid).where(
        CheckPointRule.server_id == inst.id))).scalars().all())
    assert "rl-5" not in uids
    n = len((await db_session.execute(select(CheckPointObject).where(CheckPointObject.server_id == inst.id))).all())
    assert n == 7, "物件讀不到就不清"


async def test_group_members_given_as_uids_are_named(db_session, monkeypatch) -> None:
    """不認得 dereference-group-members 的伺服器：成員是 uid 字串，要換成名稱（IP 反查靠名稱展開群組）。"""
    monkeypatch.setenv("OUTBOUND_ALLOW_CIDRS", "127.0.0.1/32")
    get_settings.cache_clear()
    srv = CheckPointMock(dereference=False)
    try:
        inst = await _server(db_session, srv)
        await cp.sync_instance(db_session, inst)
        await db_session.commit()
        grp = (await db_session.execute(select(CheckPointObject).where(
            CheckPointObject.server_id == inst.id, CheckPointObject.name == "grp-servers"))).scalar_one()
        assert grp.members == ["web-01", "db-01"]
    finally:
        srv.close()
        get_settings.cache_clear()


async def test_multi_domain_logs_in_per_domain(db_session, srv) -> None:
    await _net(db_session)
    inst = await _server(db_session, srv, domains=["DomA", "DomB"])
    await cp.sync_instance(db_session, inst)
    await db_session.commit()
    logins = [json.loads(h["body"]).get("domain") for h in srv.hits("POST", "/web_api/login")]
    assert logins == ["DomA", "DomB"]
    doms = set((await db_session.execute(select(CheckPointRule.domain).where(
        CheckPointRule.server_id == inst.id))).scalars().all())
    assert doms == {"DomA", "DomB"}


async def test_ip_detail_firewall_lookup_finds_checkpoint_rules(db_session, srv) -> None:
    from app.services.fw_lookup import rules_touching_ip
    await _net(db_session)
    inst = await _server(db_session, srv)
    await cp.sync_instance(db_session, inst)
    await db_session.commit()
    out = await rules_touching_ip(db_session, "198.51.100.10")
    mine = [r for r in out["rules"] if r["source_type"] == "checkpoint"]
    names = {r["descr"] for r in mine}
    assert "web in" in names and "servers" in names, names
    assert all(r["firewall"] == inst.name for r in mine)
    assert any(a["source_type"] == "checkpoint" and a["name"] == "grp-servers" for a in out["aliases"])


async def test_change_impact_sees_checkpoint_rules_and_nat(db_session, srv, admin_user) -> None:
    from tests.test_change_impact_engine import _rules, _run
    ips = await _net(db_session)
    inst = await _server(db_session, srv)
    await cp.sync_instance(db_session, inst)
    await db_session.commit()
    res = await _run(db_session, admin_user, ips["10"], "198.51.100.99")
    rules = _rules(res)
    assert "fw.rule_exact" in rules or "fw.rule_group" in rules, rules
    assert any(s["kind"] == "checkpoint" for s in res.manifest["sources"])
    labels = " ".join(f.subject_label for f in res.findings)
    assert "web in" in labels


async def test_api_crud_never_returns_the_key_and_delete_reclaims_nat(client, auth_headers, db_session, srv) -> None:
    await _net(db_session)
    r = await client.post("/api/v1/checkpoint/servers", headers=auth_headers, json={
        "name": "cp-api", "api_url": srv.url, "verify_tls": False, "secret": API_KEY})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_secret"] is True
    assert API_KEY not in r.text
    sid = body["id"]

    r = await client.post(f"/api/v1/checkpoint/servers/{sid}/test", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.9"
    assert r.json()["domains"][0]["gateways"] == 2

    # 留空的 secret＝不更改
    r = await client.patch(f"/api/v1/checkpoint/servers/{sid}", headers=auth_headers, json={"secret": "", "name": "cp-api2"})
    assert r.status_code == 200, r.text
    inst = await db_session.get(CheckPointServer, uuid.UUID(sid))
    await db_session.refresh(inst)
    assert cp._secret(inst) == API_KEY

    await cp.sync_instance(db_session, inst)
    await db_session.commit()
    r = await client.get(f"/api/v1/checkpoint/servers/{sid}/rules", headers=auth_headers, params={"q": "web"})
    assert r.status_code == 200, r.text
    assert {x["name"] for x in r.json()["items"]} >= {"web in", "app web"}
    rid = next(x["id"] for x in r.json()["items"] if x["name"] == "web in")
    r = await client.get(f"/api/v1/checkpoint/servers/{sid}/rules", headers=auth_headers, params={"rule_id": rid})
    assert [x["name"] for x in r.json()["items"]] == ["web in"]
    r = await client.get(f"/api/v1/checkpoint/servers/{sid}/objects", headers=auth_headers, params={"name": "grp-servers"})
    assert r.json()["items"][0]["members"] == ["web-01", "db-01"]
    r = await client.get(f"/api/v1/checkpoint/servers/{sid}/gateways", headers=auth_headers)
    assert {g["name"] for g in r.json()} == {"cp-gw-01", "cp-mgmt"}

    r = await client.delete(f"/api/v1/checkpoint/servers/{sid}", headers=auth_headers)
    assert r.status_code == 204, r.text
    db_session.expire_all()
    left = (await db_session.execute(select(NATTranslation).where(
        NATTranslation.source_origin == f"checkpoint:{sid}"))).scalars().all()
    assert left == []
    assert (await db_session.execute(select(CheckPointRule).where(
        CheckPointRule.server_id == uuid.UUID(sid)))).first() is None


async def test_api_is_admin_only(client, db_session) -> None:
    from app.core.security import hash_password
    from app.models.user import User
    from app.services.auth import issue_access_token
    u = User(username=f"ro-{uuid.uuid4().hex[:8]}", email=f"ro-{uuid.uuid4().hex[:8]}@test.local",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True, is_admin=False)
    db_session.add(u)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {issue_access_token(u)}"}
    for method, path in (("get", "/api/v1/checkpoint/servers"),
                         ("post", f"/api/v1/checkpoint/servers/{uuid.uuid4()}/sync"),
                         ("get", f"/api/v1/checkpoint/servers/{uuid.uuid4()}/rules")):
        r = await getattr(client, method)(path, headers=headers)
        assert r.status_code == 403, (path, r.status_code)
