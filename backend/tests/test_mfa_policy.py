"""MFA 政策、復原碼、防重放、驗證碼錯誤計入鎖定（2026-10-09 合規核對）。

以前：TOTP 只能個人自選，管理員無法要求；沒有復原碼（裝置遺失只能請人改資料庫）；
同一組驗證碼 90 秒內可以重複使用（防重放只是一行註解）；MFA 驗證碼錯誤不計入鎖定。
"""
from __future__ import annotations

import uuid

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import hash_password
from app.models.user import User

PASSWORD = "TestPassword2026!"


@pytest.fixture
async def c():
    from app.main import create_app
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as cl:
        yield cl


async def _user(db_session, *, admin: bool = False) -> User:
    u = User(username=f"m-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@example.invalid",
             password_hash=hash_password(PASSWORD), auth_provider="local", is_active=True, is_admin=admin)
    db_session.add(u)
    await db_session.commit()
    return u


async def _policy(db_session, required: str, sso: bool = False) -> None:
    from app.services.mfa import AuthPolicy, set_policy
    await set_policy(db_session, AuthPolicy(required, sso), updated_by=None)
    await db_session.commit()


async def _login(c, u):
    return await c.post("/api/v1/auth/login", json={"username": u.username, "password": PASSWORD})


async def _enable_totp(db_session, u: User) -> pyotp.TOTP:
    from app.services import totp
    secret = pyotp.random_base32()
    t = pyotp.TOTP(secret)
    # 用「上一個」時間步確認，讓測試可以接著用目前與下一個時間步
    assert await totp.confirm_enrollment(db_session, user=u, secret=secret, code=t.generate_otp(t.timecode(
        __import__("datetime").datetime.now(__import__("datetime").UTC)) - 1))
    return t


def _code(t: pyotp.TOTP, offset: int = 0) -> str:
    from datetime import UTC, datetime
    return t.generate_otp(t.timecode(datetime.now(UTC)) + offset)


@pytest.mark.anyio
async def test_policy_forces_setup_at_login_without_locking_out(db_session, c) -> None:
    await _policy(db_session, "all")
    u = await _user(db_session)
    r = await _login(c, u)
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_setup_required"] and body["mfa_token"] and not body["access_token"]
    assert "set-cookie" not in r.headers, "還沒完成 MFA 設定就發了工作階段"
    tok = body["mfa_token"]
    enroll = (await c.post("/api/v1/auth/mfa/setup/begin", json={"mfa_token": tok})).json()
    t = pyotp.TOTP(enroll["secret"])
    r = await c.post("/api/v1/auth/mfa/setup/confirm",
                     json={"mfa_token": tok, "secret": enroll["secret"], "code": t.now()})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["access_token"] and len(out["recovery_codes"]) == 10
    assert "jt_refresh=" in r.headers["set-cookie"]
    me = (await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {out['access_token']}"})).json()
    assert me["totp_enabled"] and me["mfa_required_by_policy"] and me["recovery_codes_remaining"] == 10


@pytest.mark.anyio
async def test_admins_policy_only_applies_to_admins(db_session, c) -> None:
    await _policy(db_session, "admins")
    plain = await _user(db_session)
    admin = await _user(db_session, admin=True)
    assert (await _login(c, plain)).json()["access_token"]
    assert (await _login(c, admin)).json()["mfa_setup_required"]


@pytest.mark.anyio
async def test_same_totp_code_cannot_be_used_twice(db_session, c) -> None:
    u = await _user(db_session)
    t = await _enable_totp(db_session, u)
    code = _code(t)
    first = (await _login(c, u)).json()
    assert first["mfa_required"]
    r = await c.post("/api/v1/auth/mfa/verify", json={"mfa_token": first["mfa_token"], "code": code})
    assert r.status_code == 200, r.text
    second = (await _login(c, u)).json()
    r = await c.post("/api/v1/auth/mfa/verify", json={"mfa_token": second["mfa_token"], "code": code})
    assert r.status_code == 401, "同一組驗證碼用了第二次（重放）"
    # 下一個時間步的碼可以
    r = await c.post("/api/v1/auth/mfa/verify", json={"mfa_token": second["mfa_token"], "code": _code(t, 1)})
    assert r.status_code == 200


@pytest.mark.anyio
async def test_recovery_code_works_once(db_session, c) -> None:
    from app.services.mfa import regenerate_recovery_codes, remaining_recovery_codes
    u = await _user(db_session)
    await _enable_totp(db_session, u)
    codes = await regenerate_recovery_codes(db_session, u)
    await db_session.commit()
    tok = (await _login(c, u)).json()["mfa_token"]
    r = await c.post("/api/v1/auth/mfa/verify", json={"mfa_token": tok, "code": codes[0].lower()})
    assert r.status_code == 200, r.text
    assert await remaining_recovery_codes(db_session, u.id) == 9
    tok = (await _login(c, u)).json()["mfa_token"]
    assert (await c.post("/api/v1/auth/mfa/verify", json={"mfa_token": tok, "code": codes[0]})).status_code == 401


@pytest.mark.anyio
async def test_wrong_mfa_codes_count_toward_lockout(db_session, c) -> None:
    u = await _user(db_session)
    t = await _enable_totp(db_session, u)
    tok = (await _login(c, u)).json()["mfa_token"]
    for _ in range(5):
        r = await c.post("/api/v1/auth/mfa/verify", json={"mfa_token": tok, "code": "000000"})
        assert r.status_code == 401
    got = (await db_session.execute(select(User).where(User.id == u.id)
                                    .execution_options(populate_existing=True))).scalar_one()
    assert got.locked_until is not None, "MFA 驗證碼錯了 5 次還沒鎖定"
    r = await c.post("/api/v1/auth/mfa/verify", json={"mfa_token": tok, "code": _code(t, 1)})
    assert r.status_code == 401, "鎖定期間正確的驗證碼也不該放行"


@pytest.mark.anyio
async def test_cannot_disable_totp_when_policy_requires_it(db_session, c) -> None:
    from app.services.auth import issue_access_token
    u = await _user(db_session)
    await _enable_totp(db_session, u)
    await _policy(db_session, "all")
    r = await c.post("/api/v1/auth/totp/disable", headers={"Authorization": f"Bearer {issue_access_token(u)}"},
                     json={"password": PASSWORD})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "mfa_required_by_policy"


@pytest.mark.anyio
async def test_regenerating_recovery_codes_needs_reauth(db_session, c) -> None:
    from app.services.auth import issue_access_token
    u = await _user(db_session)
    await _enable_totp(db_session, u)
    h = {"Authorization": f"Bearer {issue_access_token(u)}"}
    assert (await c.post("/api/v1/auth/totp/recovery-codes", headers=h, json={})).status_code == 400
    assert (await c.post("/api/v1/auth/totp/recovery-codes", headers=h,
                         json={"password": "wrong-password"})).status_code == 400
    r = await c.post("/api/v1/auth/totp/recovery-codes", headers=h, json={"password": PASSWORD})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10


@pytest.mark.anyio
async def test_admin_reset_mfa_clears_and_forces_setup_again(db_session, c) -> None:
    from app.services.auth import issue_access_token
    admin = await _user(db_session, admin=True)
    u = await _user(db_session)
    await _enable_totp(db_session, u)
    await _policy(db_session, "all")
    r = await c.post(f"/api/v1/users/{u.id}/reset-mfa", headers={"Authorization": f"Bearer {issue_access_token(admin)}"})
    assert r.status_code == 200 and r.json()["had_totp"] is True
    assert (await _login(c, u)).json()["mfa_setup_required"], "重設之後下次登入要重新設定"
    from app.models.audit import AuditLog
    assert "mfa_reset" in (await db_session.execute(select(AuditLog.action))).scalars().all()


@pytest.mark.anyio
async def test_sso_trusts_idp_unless_policy_says_otherwise(db_session) -> None:
    from app.api.v1.endpoints.auth import begin_second_step
    u = await _user(db_session)
    await _policy(db_session, "all", sso=False)
    assert await begin_second_step(db_session, u, method="oidc") is None
    await _policy(db_session, "all", sso=True)
    step = await begin_second_step(db_session, u, method="saml")
    assert step is not None and step.mfa_setup_required


@pytest.mark.anyio
async def test_policy_endpoints_are_admin_only_and_audited(db_session, c) -> None:
    from app.services.auth import issue_access_token
    admin = await _user(db_session, admin=True)
    plain = await _user(db_session)
    assert (await c.get("/api/v1/system/auth-policy", headers={"Authorization": f"Bearer {issue_access_token(plain)}"})).status_code == 403
    h = {"Authorization": f"Bearer {issue_access_token(admin)}"}
    r = await c.put("/api/v1/system/auth-policy", headers=h, json={"mfa_required": "admins", "mfa_apply_to_sso": True})
    assert r.status_code == 200
    assert (await c.get("/api/v1/system/auth-policy", headers=h)).json() == {"mfa_required": "admins", "mfa_apply_to_sso": True}
    from app.models.audit import AuditLog
    diffs = (await db_session.execute(select(AuditLog.diff).where(AuditLog.action == "update"))).scalars().all()
    assert any(isinstance(d, dict) and d.get("setting") == "auth_policy" for d in diffs)


@pytest.mark.anyio
async def test_cli_reset_mfa_recovers_the_last_admin(db_session, c) -> None:
    """唯一的管理員手機掉了、復原碼也不見 → 網頁上沒有人能幫他重設，只能從主機 shell 救回來。"""
    from app.cli.bootstrap import _reset_mfa
    from app.models.audit import AuditLog
    admin = await _user(db_session, admin=True)
    await _enable_totp(db_session, admin)
    await _policy(db_session, "admins")
    assert await _reset_mfa("nobody-" + uuid.uuid4().hex[:6]) == 1
    assert await _reset_mfa(admin.username) == 0
    fresh = (await db_session.execute(
        select(User).where(User.id == admin.id).execution_options(populate_existing=True))).scalar_one()
    assert fresh.totp_secret_enc is None and fresh.tokens_valid_after is not None
    assert (await _login(c, admin)).json()["mfa_setup_required"], "政策要求的話，下次登入要重新設定"
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.action == "mfa_reset"))).scalars().all()
    assert any((r.diff or {}).get("via") == "cli" for r in rows), "從主機重設也要留稽核"
