"""Check Point 第二階段：閘道的 Gaia API（使用者 2026-10-08：要 DHCP 租約、ARP 表之類）。

- 唯讀（預設開，有 Gaia 帳密就做）：`show-dhcp-server` → 發放範圍與發給用戶端的閘道/DNS
- 選用（預設關，要能執行指令的帳號）：`run-script` 跑**寫死的**指令讀 ARP 表與租約檔
Gaia API 沒有讀租約或 ARP 的指令（官方 Ansible 模組 114 支裡也沒有），只能走 run-script。
測試對象是照官方文件寫的 tests/checkpoint_gaia_mock.py；實機回應不同時以實機為準。
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import get_settings
from app.models.address import IPAddress
from app.models.checkpoint import CheckPointServer
from app.models.dhcp import DHCPPoolRange
from app.models.section import Section
from app.models.subnet import Subnet
from app.services import checkpoint as cp
from app.services import checkpoint_gaia as cpg
from sqlalchemy import select

from tests.checkpoint_gaia_mock import LEASES, NEIGH, PASSWORD, RO_USER, USER, GaiaMock


@pytest.fixture
def gaia(monkeypatch):
    monkeypatch.setenv("OUTBOUND_ALLOW_CIDRS", "127.0.0.1/32")
    get_settings.cache_clear()
    s = GaiaMock()
    yield s
    s.close()
    get_settings.cache_clear()


async def _server(db) -> CheckPointServer:  # type: ignore[no-untyped-def]
    srv = CheckPointServer(name=f"cp-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.2", verify_tls=False)
    db.add(srv)
    await db.flush()
    srv.secret_enc, srv.secret_nonce = cp.encrypt_secret_for(srv.id, "unused-key")
    await db.commit()
    return srv


async def _target(db, gaia, *, user: str = USER, **kw):  # type: ignore[no-untyped-def]
    from app.models.checkpoint_gaia import CheckPointGaiaTarget
    srv = kw.pop("server", None) or await _server(db)
    t = CheckPointGaiaTarget(server_id=srv.id, name=f"gw-{uuid.uuid4().hex[:6]}", gaia_url=gaia.url,
                             username=user, verify_tls=False, **kw)
    db.add(t)
    await db.flush()
    t.secret_enc, t.secret_nonce = cpg.encrypt_secret_for(t.id, PASSWORD)
    await db.commit()
    return t


async def _net(db) -> dict[str, IPAddress]:  # type: ignore[no-untyped-def]
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    out = {}
    for last in (1, 10, 20, 21, 30):
        ip = IPAddress(subnet_id=sub.id, ip=f"198.51.100.{last}", state="active")
        db.add(ip)
        out[str(last)] = ip
    await db.commit()
    return out


# ─────────────────── 解析 ───────────────────

def test_dhcp_server_pools_subtract_exclusions_and_skip_disabled() -> None:
    from tests.checkpoint_gaia_mock import DHCP
    subs = {s["subnet"]: s for s in cpg.parse_dhcp_server(DHCP)}
    lan = subs["198.51.100.0/24"]
    assert lan["enabled"] is True
    assert lan["pools"] == [("198.51.100.100", "198.51.100.119"), ("198.51.100.126", "198.51.100.150")]
    assert lan["default_gateway"] == "198.51.100.1"
    assert lan["dns_servers"] == ["198.51.100.53", "198.51.100.54"]
    assert lan["domain_name"] == "lab.example.test"
    assert subs["203.0.113.0/25"]["enabled"] is False
    # 伺服器整個關掉：每個子網路都算停用
    assert all(s["enabled"] is False for s in cpg.parse_dhcp_server({**DHCP, "enabled": False}))


def test_neigh_uses_the_confirmation_age_and_skips_permanent_and_failed() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    rows = {r["ip"]: r for r in cpg.parse_neigh(NEIGH, now=now)}
    assert set(rows) == {"198.51.100.10", "198.51.100.20", "198.51.100.30", "192.0.2.254", "fe80::1"}
    assert rows["198.51.100.10"]["seen_at"] == now - timedelta(seconds=8)
    assert rows["198.51.100.20"]["seen_at"] == now - timedelta(seconds=540), "STALE 也有確認時間：照實記"
    assert rows["198.51.100.30"]["permanent"] is True
    assert rows["198.51.100.10"]["mac"] == "00:00:5e:00:53:10"
    # 沒有計時資訊、又不是 REACHABLE：不當上線證據
    assert rows["fe80::1"]["seen_at"] is None


def test_neigh_without_stats_trusts_only_reachable() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    text = ("198.51.100.10 dev eth1 lladdr 00:00:5e:00:53:10 REACHABLE\n"
            "198.51.100.20 dev eth1 lladdr 00:00:5e:00:53:20 STALE\n")
    rows = {r["ip"]: r for r in cpg.parse_neigh(text, now=now)}
    assert rows["198.51.100.10"]["seen_at"] == now
    assert rows["198.51.100.20"]["seen_at"] is None


def test_leases_take_the_last_record_and_only_active_unexpired() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    rows = {r["ip"]: r for r in cpg.parse_leases(LEASES, now=now)}
    assert set(rows) == {"198.51.100.10", "198.51.100.21"}
    assert rows["198.51.100.10"]["hostname"] == "web-01"
    assert rows["198.51.100.21"]["hostname"] == 'printer "lobby"'
    assert rows["198.51.100.21"]["mac"] == "00:00:5e:00:53:21"


def test_only_fixed_scripts_can_run() -> None:
    assert set(cpg.SCRIPTS) == {"arp", "leases", "probe"}
    for script in cpg.SCRIPTS.values():
        assert "{" not in script and "%" not in script     # 沒有樣板欄位可以塞東西
    with pytest.raises(KeyError):
        cpg.script_for("rm -rf /")


# ─────────────────── 同步 ───────────────────

async def test_read_only_sync_reads_dhcp_and_never_runs_scripts(db_session, gaia) -> None:
    from app.models.checkpoint_gaia import CheckPointDhcpSubnet
    await _net(db_session)
    t = await _target(db_session, gaia)
    assert t.allow_scripts is False and t.sync_dhcp is True, "預設：只做唯讀的部分"
    summary = await cpg.sync_target(db_session, t)
    await db_session.commit()
    assert "errors" not in summary, summary
    assert gaia.hits("POST", "/gaia_api/run-script") == []
    login = json.loads(gaia.hits("POST", "/gaia_api/login")[0]["body"])
    assert login == {"user": USER, "password": PASSWORD}
    assert gaia.logged_out == ["gsid-1"]
    pools = sorted((p.start_ip, p.end_ip) for p in (await db_session.execute(select(DHCPPoolRange).where(
        DHCPPoolRange.source_type == "checkpoint", DHCPPoolRange.source_id == t.id))).scalars().all())
    assert pools == [("198.51.100.100", "198.51.100.119"), ("198.51.100.126", "198.51.100.150")]
    mirror = {m.subnet_cidr: m for m in (await db_session.execute(select(CheckPointDhcpSubnet).where(
        CheckPointDhcpSubnet.target_id == t.id))).scalars().all()}
    assert str(mirror["198.51.100.0/24"].default_gateway) == "198.51.100.1"
    assert [str(x) for x in mirror["198.51.100.0/24"].dns_servers] == ["198.51.100.53", "198.51.100.54"]
    assert mirror["203.0.113.0/25"].enabled is False
    await db_session.refresh(t)
    assert t.api_version == "1.6" and t.last_error is None and t.last_sync_at is not None


async def test_scripts_read_arp_and_leases_when_allowed(db_session, gaia) -> None:
    ips = await _net(db_session)
    t = await _target(db_session, gaia, allow_scripts=True)
    summary = await cpg.sync_target(db_session, t)
    await db_session.commit()
    assert "errors" not in summary, summary
    # 只跑寫死的指令
    assert gaia.scripts == [cpg.SCRIPTS["arp"], cpg.SCRIPTS["leases"]]
    for k in ("10", "20", "30", "21"):
        await db_session.refresh(ips[k])
    assert "arp:checkpoint" in (ips["10"].arp_seen or {})
    seen10 = datetime.fromisoformat(ips["10"].arp_seen["arp:checkpoint"])
    assert datetime.now(UTC) - seen10 < timedelta(seconds=60)
    seen20 = datetime.fromisoformat(ips["20"].arp_seen["arp:checkpoint"])
    assert timedelta(seconds=500) < datetime.now(UTC) - seen20 < timedelta(seconds=600), "推回真正確認的時間"
    assert "arp:checkpoint" not in (ips["30"].arp_seen or {}), "PERMANENT 不算上線證據"
    # 租約：旗標、MAC、主機名稱
    assert ips["10"].in_dhcp_lease is True and ips["21"].in_dhcp_lease is True
    assert "lease:checkpoint" in (ips["21"].arp_seen or {})
    from app.models.ip_hostname import IPHostnameObservation
    obs = {(o.ip_id, o.hostname) for o in (await db_session.execute(select(IPHostnameObservation).where(
        IPHostnameObservation.source == "checkpoint"))).scalars().all()}
    assert (ips["10"].id, "web-01") in obs
    assert summary["arp_rows"] == 5 and summary["leases"] == 2


async def test_plain_text_script_output_also_works(db_session, gaia) -> None:
    gaia.base64_output = False
    ips = await _net(db_session)
    t = await _target(db_session, gaia, allow_scripts=True)
    await cpg.sync_target(db_session, t)
    await db_session.commit()
    await db_session.refresh(ips["10"])
    assert "arp:checkpoint" in (ips["10"].arp_seen or {})


async def test_a_read_only_account_still_gets_dhcp_and_says_why_scripts_failed(db_session, gaia) -> None:
    await _net(db_session)
    t = await _target(db_session, gaia, user=RO_USER, allow_scripts=True)
    summary = await cpg.sync_target(db_session, t)
    await db_session.commit()
    assert summary["pools"] == 2
    assert set(summary["errors"]) == {"arp", "leases"}
    await db_session.refresh(t)
    assert t.last_error and "arp" in t.last_error


async def test_oversized_lease_file_keeps_the_previous_lease_flags(db_session, gaia) -> None:
    ips = await _net(db_session)
    t = await _target(db_session, gaia, allow_scripts=True)
    await cpg.sync_target(db_session, t)
    await db_session.commit()
    gaia.outputs["dhcpd.leases"] = "JTIPAM_SIZE 999999999999\n"
    summary = await cpg.sync_target(db_session, t)
    await db_session.commit()
    assert "leases" in summary["errors"]
    await db_session.refresh(ips["10"])
    assert ips["10"].in_dhcp_lease is True, "讀不到就不清"


async def test_an_empty_lease_file_is_authoritative(db_session, gaia) -> None:
    """實機：DHCP 伺服器沒開時租約檔存在但是空的（`JTIPAM_SIZE 0`）—— 那就是「沒有租約」，要清掉舊標記。"""
    ips = await _net(db_session)
    t = await _target(db_session, gaia, allow_scripts=True)
    await cpg.sync_target(db_session, t)
    await db_session.commit()
    gaia.outputs["dhcpd.leases"] = "JTIPAM_SIZE 0\n"
    summary = await cpg.sync_target(db_session, t)
    await db_session.commit()
    assert "errors" not in summary and summary["leases"] == 0
    await db_session.refresh(ips["10"])
    assert ips["10"].in_dhcp_lease is False


async def test_missing_lease_file_is_an_error_not_an_empty_list(db_session, gaia) -> None:
    ips = await _net(db_session)
    t = await _target(db_session, gaia, allow_scripts=True)
    await cpg.sync_target(db_session, t)
    await db_session.commit()
    gaia.outputs["dhcpd.leases"] = "JTIPAM_SIZE -1\n"
    summary = await cpg.sync_target(db_session, t)
    await db_session.commit()
    assert "leases" in summary["errors"]
    await db_session.refresh(ips["10"])
    assert ips["10"].in_dhcp_lease is True, "讀不到檔案不等於沒有租約"


async def test_wrong_password_is_a_clear_error_without_the_password(db_session, gaia) -> None:
    t = await _target(db_session, gaia)
    t.secret_enc, t.secret_nonce = cpg.encrypt_secret_for(t.id, "wrong-password")
    await db_session.commit()
    with pytest.raises(cpg.GaiaError) as ei:
        await cpg.sync_target(db_session, t)
    assert ei.value.code == "cpg_login_failed"
    assert "wrong-password" not in str(ei.value) and PASSWORD not in str(ei.value)


async def test_diagnose_reports_version_dhcp_and_script_permission(db_session, gaia) -> None:
    t = await _target(db_session, gaia)
    out = await cpg.diagnose(t)
    assert out["version"] == "1.6" and out["dhcp_subnets"] == 2
    assert out["scripts"] == "not_enabled"
    assert gaia.hits("POST", "/gaia_api/run-script") == []
    t.allow_scripts = True
    assert (await cpg.diagnose(t))["scripts"] == "ok"
    ro = await _target(db_session, gaia, user=RO_USER, allow_scripts=True)
    out = await cpg.diagnose(ro)
    assert out["scripts"] == "denied"


# ─────────────────── API／刪除／評估 ───────────────────

async def test_api_crud_never_returns_the_password_and_delete_reclaims(client, auth_headers, db_session, gaia) -> None:
    ips = await _net(db_session)
    srv = await _server(db_session)
    r = await client.post(f"/api/v1/checkpoint/servers/{srv.id}/gaia-targets", headers=auth_headers, json={
        "name": "gw-api", "gaia_url": gaia.url, "username": USER, "secret": PASSWORD, "verify_tls": False,
        "allow_scripts": True})
    assert r.status_code == 201, r.text
    assert PASSWORD not in r.text and r.json()["has_secret"] is True
    tid = r.json()["id"]
    r = await client.get(f"/api/v1/checkpoint/servers/{srv.id}/gaia-targets", headers=auth_headers)
    assert [x["name"] for x in r.json()] == ["gw-api"]
    r = await client.post(f"/api/v1/checkpoint/gaia-targets/{tid}/test", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["scripts"] == "ok"
    r = await client.patch(f"/api/v1/checkpoint/gaia-targets/{tid}", headers=auth_headers, json={"secret": ""})
    assert r.status_code == 200, r.text
    from app.models.checkpoint_gaia import CheckPointGaiaTarget
    t = await db_session.get(CheckPointGaiaTarget, uuid.UUID(tid))
    await db_session.refresh(t)
    await cpg.sync_target(db_session, t)
    await db_session.commit()
    r = await client.get(f"/api/v1/checkpoint/gaia-targets/{tid}/dhcp", headers=auth_headers)
    assert {x["subnet_cidr"] for x in r.json()} == {"198.51.100.0/24", "203.0.113.0/25"}

    r = await client.delete(f"/api/v1/checkpoint/gaia-targets/{tid}", headers=auth_headers)
    assert r.status_code == 204, r.text
    db_session.expire_all()
    assert (await db_session.execute(select(DHCPPoolRange).where(
        DHCPPoolRange.source_type == "checkpoint"))).first() is None
    await db_session.refresh(ips["10"])
    assert ips["10"].in_dhcp_lease is False, "刪掉整合要收回它寫的租約旗標"


async def test_deleting_the_management_server_reclaims_gateway_data(client, auth_headers, db_session, gaia) -> None:
    await _net(db_session)
    t = await _target(db_session, gaia)
    await cpg.sync_target(db_session, t)
    await db_session.commit()
    r = await client.delete(f"/api/v1/checkpoint/servers/{t.server_id}", headers=auth_headers)
    assert r.status_code == 204, r.text
    db_session.expire_all()
    assert (await db_session.execute(select(DHCPPoolRange).where(
        DHCPPoolRange.source_type == "checkpoint"))).first() is None


async def test_gaia_api_is_admin_only(client, db_session) -> None:
    from app.core.security import hash_password
    from app.models.user import User
    from app.services.auth import issue_access_token
    u = User(username=f"ro-{uuid.uuid4().hex[:8]}", email=f"ro-{uuid.uuid4().hex[:8]}@test.local",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True, is_admin=False)
    db_session.add(u)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {issue_access_token(u)}"}
    for method, path in (("get", f"/api/v1/checkpoint/servers/{uuid.uuid4()}/gaia-targets"),
                         ("post", f"/api/v1/checkpoint/gaia-targets/{uuid.uuid4()}/sync"),
                         ("get", f"/api/v1/checkpoint/gaia-targets/{uuid.uuid4()}/dhcp")):
        r = await getattr(client, method)(path, headers=headers)
        assert r.status_code == 403, (path, r.status_code)


async def test_change_impact_sees_the_gateway_handed_out_by_check_point_dhcp(db_session, gaia, admin_user) -> None:
    from tests.test_change_impact_engine import _rules, _run
    ips = await _net(db_session)
    t = await _target(db_session, gaia)
    await cpg.sync_target(db_session, t)
    await db_session.commit()
    res = await _run(db_session, admin_user, ips["1"], "198.51.100.99")
    assert "dhcp.scope_router" in _rules(res), _rules(res)
    assert any(s["kind"] == "checkpoint" and s["id"] == str(t.id) for s in res.manifest["sources"])
