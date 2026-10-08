"""ISOinsight 整合的 API：權限、驗證、秘密不回傳、稽核、測試與預覽、排程開啟條件、租約查詢的 RBAC。"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.models.audit import AuditLog
from app.models.customer import Customer
from app.models.isoinsight import IsoInsightLease, IsoInsightSource
from app.models.permission import Permission
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User
from app.services.isoinsight import job
from sqlalchemy import select

from tests.isoinsight_mock import MockServer, lease_body

PW = "Pa ss&w0rd=中"
TPE = ZoneInfo("Asia/Taipei")


def _t(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).astimezone(TPE).strftime("%Y/%m/%d %H:%M:%S")


@pytest.fixture
def srv(monkeypatch):
    s = MockServer()
    s.route("POST", "/api/logon", (200, [("Set-Cookie", "SID=abc; Path=/")], b"{}"))
    s.route("GET", "/isosvc", lease_body([
        {"ip": "192.0.2.20", "mac": "02:00:5e:00:53:20", "name": "laptop-07", "start_time": _t(-1),
         "end_time": _t(3)}]))
    # 本機 mock 伺服器在 127.0.0.1：正式的出站政策擋迴路位址（client 的測試另外確認這件事）
    monkeypatch.setattr(job, "OUTBOUND_CHECK", lambda _h, _a: None)
    yield s
    s.close()


async def _subnet(db, cidr="192.0.2.0/24", customer=None) -> Subnet:
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr=cidr, customer_id=customer.id if customer else None)
    db.add(sub)
    await db.commit()
    return sub


def _payload(srv: MockServer, sub: Subnet, **kw) -> dict:
    return {"name": f"iso-{uuid.uuid4().hex[:6]}", "base_url": srv.url, "username": "operator", "password": PW,
            "scope_subnet_ids": [str(sub.id)], "timezone_confirmed": True, **kw}


async def _nonadmin(db, *, subnet: Subnet | None = None) -> dict:
    from app.core.security import hash_password
    from app.services.auth import issue_access_token
    u = User(username=f"na-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local", display_name="NA",
             password_hash=hash_password("TestPassword2026!"), auth_provider="local", is_active=True, is_admin=False)
    db.add(u)
    await db.flush()
    if subnet is not None:
        db.add(Permission(object_type="subnet", object_id=subnet.id, principal_type="user", principal_id=u.id,
                          level="read"))
    await db.commit()
    return {"Authorization": f"Bearer {issue_access_token(u)}"}


async def test_create_hides_the_password_and_audits_without_it(client, auth_headers, db_session, srv) -> None:
    sub = await _subnet(db_session)
    r = await client.post("/api/v1/isoinsight/sources", headers=auth_headers, json=_payload(srv, sub))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_password"] is True and "password" not in body and "password_enc" not in body
    assert body["login_method"] == "POST" and body["post_format"] == "form" and body["auth_mode"] == "cookie"
    # 排程預設開啟（使用者 2026-10-08）：預覽成功之前排程不會同步，見 test_schedule_is_on_by_default…
    assert body["schedule_enabled"] is True and body["run_on"] == "server"
    assert PW not in r.text
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.object_type == "isoinsight_source"))).scalars().all()
    assert [a.action for a in logs] == ["create"]
    assert PW not in json.dumps(logs[0].diff, ensure_ascii=False)
    # 讀回清單也不會有密碼
    r = await client.get("/api/v1/isoinsight/sources", headers=auth_headers)
    assert r.status_code == 200 and PW not in r.text and r.json()["total"] == 1


async def test_password_keeps_its_whitespace(client, auth_headers, db_session, srv) -> None:
    from app.services.isoinsight import config as icfg
    sub = await _subnet(db_session)
    r = await client.post("/api/v1/isoinsight/sources", headers=auth_headers, json=_payload(srv, sub, password=" pw "))
    src = await db_session.get(IsoInsightSource, uuid.UUID(r.json()["id"]))
    assert icfg.decrypt_password(src) == " pw "


@pytest.mark.parametrize(("patch", "code"), [
    ({"base_url": "https://u:p@192.0.2.10"}, None),
    ({"base_url": "https://192.0.2.10/?x=1"}, None),
    ({"login_path": "//evil.example/api"}, None),
    ({"login_path": "/api/logon?user=x"}, None),
    ({"lease_path": "https://198.51.100.1/isosvc"}, None),
    ({"auth_mode": "token"}, None),
    ({"token_header": "Cookie", "auth_mode": "token", "token_path": "data.token"}, None),
    ({"source_timezone": "Mars/Base"}, None),
    ({"scope_subnet_ids": []}, None),
    ({"timezone_confirmed": False}, "isoinsight_timezone_unconfirmed"),
    ({"sync_interval_seconds": 30}, None),
])
async def test_create_validation(client, auth_headers, db_session, srv, patch, code) -> None:
    sub = await _subnet(db_session)
    r = await client.post("/api/v1/isoinsight/sources", headers=auth_headers, json=_payload(srv, sub, **patch))
    assert r.status_code == 422, r.text
    if code:
        assert r.json()["detail"]["code"] == code


async def test_scope_cannot_mix_tenants(client, auth_headers, db_session, srv) -> None:
    ca = Customer(name=f"c-{uuid.uuid4().hex[:4]}")
    db_session.add(ca)
    await db_session.commit()
    mine = await _subnet(db_session, customer=ca)
    nobody = await _subnet(db_session, "198.51.100.0/24")
    p = _payload(srv, mine, customer_id=str(ca.id))
    p["scope_subnet_ids"].append(str(nobody.id))
    r = await client.post("/api/v1/isoinsight/sources", headers=auth_headers, json=p)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "isoinsight_scope_mixed_tenant"
    r = await client.post("/api/v1/isoinsight/sources", headers=auth_headers,
                          json=_payload(srv, mine))      # 沒指定客戶，子網路卻屬於某客戶
    assert r.status_code == 422 and r.json()["detail"]["code"] == "isoinsight_scope_mixed_tenant"


async def test_non_admins_cannot_manage_or_read_sources(client, db_session, srv) -> None:
    sub = await _subnet(db_session)
    h = await _nonadmin(db_session, subnet=sub)
    assert (await client.get("/api/v1/isoinsight/sources", headers=h)).status_code == 403
    assert (await client.post("/api/v1/isoinsight/sources", headers=h, json=_payload(srv, sub))).status_code == 403


async def _create(client, auth_headers, srv, sub, **kw) -> dict:
    r = await client.post("/api/v1/isoinsight/sources", headers=auth_headers, json=_payload(srv, sub, **kw))
    assert r.status_code == 201, r.text
    return r.json()


async def _scheduled(src_id: str, days: int = 1) -> dict:
    """跑一輪排程（時間往後推 days 天，超過間隔），回傳這個來源的結果（沒排到就是 None）。"""
    res = await job.run_due(None, datetime.now(UTC) + timedelta(days=days), check=lambda *_a, **_k: None)
    return next((r for r in res if str(r["id"]) == src_id), None)


async def test_schedule_is_on_by_default_but_waits_for_a_successful_preview(client, auth_headers, db_session,
                                                                           srv) -> None:
    """使用者 2026-10-08：排程預設開啟，只是還沒成功預覽過時不會同步。
    開關隨時可以切；排程輪到時沒有預覽就記「要先預覽」（不連線、不留同步記錄），改了帳密要重新預覽。"""
    sub = await _subnet(db_session)
    src = await _create(client, auth_headers, srv, sub)
    url = f"/api/v1/isoinsight/sources/{src['id']}"
    assert src["schedule_enabled"] is True and src["preview_ok_at"] is None
    r = await client.patch(url, headers=auth_headers, json={"schedule_enabled": False})
    assert r.status_code == 200 and r.json()["schedule_enabled"] is False
    r = await client.patch(url, headers=auth_headers, json={"schedule_enabled": True})
    assert r.status_code == 200 and r.json()["schedule_enabled"] is True
    hits = len(srv.requests)
    out = await _scheduled(src["id"])
    assert out["result"] == "skipped" and out["error_code"] == "PREVIEW_REQUIRED"
    assert len(srv.requests) == hits, "沒有預覽就不可以連到來源"
    body = (await client.get(url, headers=auth_headers)).json()
    assert body["last_error_code"] == "PREVIEW_REQUIRED" and body["last_result"] == "skipped"
    assert (await client.get(f"{url}/runs", headers=auth_headers)).json()["items"] == []

    r = await client.post(f"{url}/test", headers=auth_headers, json={})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["ok"] is True and [s["stage"] for s in t["stages"]] == ["login", "fetch", "validate"]
    assert PW not in r.text and "abc" not in json.dumps(t["stages"])
    r = await client.post(f"{url}/preview", headers=auth_headers)
    assert r.status_code == 200 and r.json()["applied"] is False
    assert r.json()["counts"]["created"] == 1
    r = await client.patch(url, headers=auth_headers, json={"schedule_enabled": True})
    assert r.status_code == 200 and r.json()["schedule_enabled"] is True and r.json()["preview_ok_at"]

    # 密碼留空＝保留；改密碼＝要重新測試／預覽，舊的成功不算
    before = r.json()["config_version"]
    r = await client.patch(url, headers=auth_headers, json={"password": ""})
    assert r.status_code == 200 and r.json()["preview_ok_at"] is not None
    r = await client.patch(url, headers=auth_headers, json={"password": "new-secret"})
    body = r.json()
    assert body["preview_ok_at"] is None and body["last_test_ok_at"] is None
    assert body["config_version"] == before + 1
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "update"))).scalars().all()
    assert any((a.diff or {}).get("password_changed") is True for a in logs)
    assert "new-secret" not in json.dumps([a.diff for a in logs])
    # 排程開關留著，但在重新預覽前輪到也只記「要先預覽」
    hits = len(srv.requests)
    assert (await _scheduled(src["id"], days=2))["error_code"] == "PREVIEW_REQUIRED"
    assert len(srv.requests) == hits


async def test_tls_off_is_recorded(client, auth_headers, db_session, srv) -> None:
    sub = await _subnet(db_session)
    src = await _create(client, auth_headers, srv, sub)
    r = await client.patch(f"/api/v1/isoinsight/sources/{src['id']}", headers=auth_headers,
                           json={"verify_tls": False})
    assert r.status_code == 200 and r.json()["verify_tls"] is False
    a = (await db_session.execute(select(AuditLog).where(AuditLog.action == "update"))).scalars().one()
    assert a.diff["verify_tls"] is False


async def test_editing_credentials_releases_the_auth_hold(client, auth_headers, db_session, srv) -> None:
    sub = await _subnet(db_session)
    src = await _create(client, auth_headers, srv, sub)
    row = await db_session.get(IsoInsightSource, uuid.UUID(src["id"]))
    row.auth_hold = True
    await db_session.commit()
    r = await client.patch(f"/api/v1/isoinsight/sources/{src['id']}", headers=auth_headers,
                           json={"username": "operator2"})
    assert r.json()["auth_hold"] is False


async def test_manual_sync_runs_in_the_background_and_is_audited(client, auth_headers, db_session, srv) -> None:
    import asyncio

    from app.services.background_tasks import _BG_TASKS
    sub = await _subnet(db_session)
    src = await _create(client, auth_headers, srv, sub)
    r = await client.post(f"/api/v1/isoinsight/sources/{src['id']}/sync", headers=auth_headers)
    assert r.status_code == 200, r.text
    for t in list(_BG_TASKS):
        await asyncio.wait_for(t, 30)
    runs = (await client.get(f"/api/v1/isoinsight/sources/{src['id']}/runs", headers=auth_headers)).json()
    assert runs["total"] == 1 and runs["items"][0]["result"] == "success" and runs["items"][0]["created"] == 1
    acts = (await db_session.execute(select(AuditLog.action).where(
        AuditLog.object_type == "isoinsight_source"))).scalars().all()
    assert "sync" in acts


async def test_sync_while_running_is_409(client, auth_headers, db_session, srv) -> None:
    sub = await _subnet(db_session)
    src = await _create(client, auth_headers, srv, sub)
    row = await db_session.get(IsoInsightSource, uuid.UUID(src["id"]))
    row.running_since = datetime.now(UTC)
    row.running_token = uuid.uuid4()
    await db_session.commit()
    for path in ("sync", "test", "preview"):
        r = await client.post(f"/api/v1/isoinsight/sources/{src['id']}/{path}", headers=auth_headers)
        assert r.status_code == 409, (path, r.text)
        assert r.json()["detail"]["code"] == "isoinsight_sync_already_running"


async def test_lease_listing_follows_subnet_visibility(client, auth_headers, db_session, srv) -> None:
    mine = await _subnet(db_session, "192.0.2.0/24")
    other = await _subnet(db_session, "198.51.100.0/24")
    src = await _create(client, auth_headers, srv, mine)
    sid = uuid.UUID(src["id"])
    now = datetime.now(UTC)
    for ip, subnet in (("192.0.2.20", mine), ("198.51.100.20", other), ("203.0.113.9", None)):
        db_session.add(IsoInsightLease(source_id=sid, ip=ip, mac_key="", subnet_id=subnet.id if subnet else None,
                                       match_status="matched" if subnet else "no_subnet",
                                       start_at=now - timedelta(hours=1), end_at=now + timedelta(hours=1),
                                       first_observed_at=now, lease_observed_at=now))
    await db_session.commit()
    h = await _nonadmin(db_session, subnet=mine)
    r = await client.get("/api/v1/isoinsight/leases", headers=h)
    assert r.status_code == 200, r.text
    assert [i["ip"] for i in r.json()["items"]] == ["192.0.2.20"] and r.json()["total"] == 1
    assert r.json()["items"][0]["state"] == "active"
    r = await client.get("/api/v1/isoinsight/leases", headers=auth_headers)
    assert r.json()["total"] == 3
    r = await client.get("/api/v1/isoinsight/leases", headers=auth_headers, params={"match_status": "no_subnet"})
    assert [i["ip"] for i in r.json()["items"]] == ["203.0.113.9"]
    nobody = await _nonadmin(db_session)
    assert (await client.get("/api/v1/isoinsight/leases", headers=nobody)).json()["total"] == 0
