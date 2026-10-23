"""憑證推送引擎（0227）：推上去 → 連過去確認 → 確認不到就換回去；自動推送挑哪些目標；稽核不帶私鑰。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.cert_push_target import CertPushTarget
from app.services.cert_push import engine
from app.services.cert_push.drivers import DRIVERS
from tests.test_cert_push_drivers import FakeFortiGate

pytestmark = pytest.mark.anyio


async def _cert_with_version(client, auth_headers) -> tuple[str, str]:
    from tests.test_certificates_api import _create_cert, _files, _make_cert
    cid = await _create_cert(client, auth_headers)
    cert_pem, key_pem = _make_cert("fw.example.com", ("fw.example.com",))
    r = await client.post(f"/api/v1/certificates/{cid}/versions", headers=auth_headers, files=_files(cert_pem, key_pem))
    assert r.status_code in (200, 201), r.text
    return cid, r.json()["fingerprint_sha256"]


async def _target(db, cid: str, **kw) -> CertPushTarget:
    t = CertPushTarget(name=f"fg-{uuid.uuid4().hex[:6]}", certificate_id=uuid.UUID(cid), kind="fortigate",
                       api_url="https://192.0.2.1:8443", options={"bind": ["admin"]}, **kw)
    db.add(t)
    await db.flush()
    await engine.save_secret(db, t.id, {"api_key": "tok"})
    await db.commit()
    return t


@pytest.fixture
def fake_fg(monkeypatch):
    dev = FakeFortiGate()
    monkeypatch.setattr(engine, "make_http", lambda target, secret: dev)
    monkeypatch.setattr(engine, "VERIFY_INTERVAL_S", 0)
    return dev


def _probe(seq):
    it = iter(seq)

    async def probe(host, port, timeout=8.0):
        try:
            return next(it)
        except StopIteration:
            return seq[-1]
    return probe


async def test_push_verified(client, auth_headers, db_session, fake_fg) -> None:
    cid, fp = await _cert_with_version(client, auth_headers)
    t = await _target(db_session, cid)
    r = await engine.push_target(db_session, t, probe=_probe(["0" * 64, fp]))
    assert r["status"] == "ok"
    assert t.last_fingerprint == fp and t.last_status == "ok"
    assert fake_fg.global_cfg["admin-server-cert"].startswith("jtipam-")
    # 稽核：有推送記錄，不含私鑰
    await db_session.commit()
    row = (await db_session.execute(select(AuditLog).where(AuditLog.action == "cert_push",
                                                           AuditLog.object_id == t.id))).scalar_one()
    assert "PRIVATE KEY" not in str(row.diff) and row.diff["status"] == "ok"


async def test_verify_failure_rolls_back(client, auth_headers, db_session, fake_fg) -> None:
    cid, fp = await _cert_with_version(client, auth_headers)
    t = await _target(db_session, cid)
    r = await engine.push_target(db_session, t, probe=_probe(["0" * 64]))   # 推之前連得到，推完一直是舊的
    assert r["status"] == "rolled_back"
    assert fake_fg.global_cfg["admin-server-cert"] == "self-sign"
    assert t.last_fingerprint is None and t.last_error == "certpush_verify_failed"


async def test_unreachable_before_and_after_is_unverified_not_rolled_back(client, auth_headers, db_session, fake_fg) -> None:
    cid, fp = await _cert_with_version(client, auth_headers)
    t = await _target(db_session, cid)
    r = await engine.push_target(db_session, t, probe=_probe([None]))
    assert r["status"] == "unverified"
    assert fake_fg.global_cfg["admin-server-cert"].startswith("jtipam-")
    assert t.last_fingerprint == fp


async def test_device_error_is_recorded_with_step(client, auth_headers, db_session, fake_fg) -> None:
    cid, _ = await _cert_with_version(client, auth_headers)
    t = await _target(db_session, cid)
    fake_fg.fail_on = "/local/import"
    r = await engine.push_target(db_session, t, probe=_probe(["0" * 64]))
    assert r["status"] == "error" and t.last_error == "certpush_device_error"
    assert any(s["step"] == "import" and not s["ok"] for s in t.last_steps)


async def test_no_current_version(db_session, client, auth_headers, fake_fg) -> None:
    from tests.test_certificates_api import _create_cert
    cid = await _create_cert(client, auth_headers)
    t = await _target(db_session, cid)
    r = await engine.push_target(db_session, t, probe=_probe([None]))
    assert r["status"] == "error" and r["error"] == "certpush_no_version"


async def test_due_targets_rules(client, auth_headers, db_session) -> None:
    cid, fp = await _cert_with_version(client, auth_headers)
    now = datetime.now(UTC)
    fresh = await _target(db_session, cid, auto_push=True)                                   # 沒推過
    done = await _target(db_session, cid, auto_push=True, last_fingerprint=fp)               # 已是目前版本
    manual = await _target(db_session, cid, auto_push=False)                                 # 沒開自動
    recent_fail = await _target(db_session, cid, auto_push=True, last_status="error",
                                last_push_at=now - timedelta(minutes=5))                    # 剛失敗，先別再敲
    old_fail = await _target(db_session, cid, auto_push=True, last_status="error",
                             last_push_at=now - timedelta(hours=2))
    ids = {t.id for t in await engine.due_targets(db_session, now)}
    assert fresh.id in ids and old_fail.id in ids
    assert not ids & {done.id, manual.id, recent_fail.id}


async def test_secret_round_trip_and_aad(db_session, client, auth_headers) -> None:
    cid, _ = await _cert_with_version(client, auth_headers)
    t = await _target(db_session, cid)
    assert await engine.load_secret(db_session, t.id) == {"api_key": "tok"}


def test_every_kind_has_a_driver_and_auth() -> None:
    from app.models.cert_push_target import PUSH_KINDS
    for k in PUSH_KINDS:
        assert k in DRIVERS
        assert engine._auth_headers(k, {"api_key": "x", "api_secret": "y"})
