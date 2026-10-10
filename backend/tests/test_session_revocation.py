"""伺服器端工作階段（2026-10-09 合規核對）。

以前：更新權杖是不受追蹤的 JWT，14 天內一直有效；登出什麼都不做；改密碼、停用帳號、管理員
介入都收不回已發出的權杖；權杖存在 localStorage（任何 XSS 都讀得到）。
現在：更新權杖只在 HttpOnly Cookie、每次換發換新、重用偵測；存取權杖帶 sid，撤銷立即生效。
"""
from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import hash_password
from app.models.user import User
from app.models.user_session import UserSession
from app.services import sessions

PASSWORD = "TestPassword2026!"
H = {sessions.CSRF_HEADER: sessions.CSRF_VALUE}


@pytest.fixture
async def https_client():
    from app.main import create_app
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as c:
        yield c


async def _user(db_session, *, admin: bool = False, name: str | None = None) -> User:
    u = User(username=name or f"u-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@example.invalid",
             password_hash=hash_password(PASSWORD), auth_provider="local", is_active=True, is_admin=admin)
    db_session.add(u)
    await db_session.commit()
    return u


async def _login(c: AsyncClient, user: User) -> str:
    r = await c.post("/api/v1/auth/login", json={"username": user.username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


async def _refresh_with(c: AsyncClient, raw: str):
    """模擬另一個分頁／攻擊者拿著某一把更新權杖來換發（不經過 cookie jar）。"""
    c.cookies.clear()
    return await c.post("/api/v1/auth/refresh", headers={**H, "Cookie": f"{sessions.REFRESH_COOKIE}={raw}"})


@pytest.mark.anyio
async def test_login_puts_refresh_token_only_in_httponly_cookie(db_session, https_client) -> None:
    u = await _user(db_session)
    r = await https_client.post("/api/v1/auth/login", json={"username": u.username, "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"] is None, "更新權杖不可以出現在 JavaScript 讀得到的本文"
    cookie = r.headers["set-cookie"]
    for attr in ("jt_refresh=", "HttpOnly", "Secure", "SameSite=strict", "Path=/api/v1/auth"):
        assert attr.lower() in cookie.lower(), f"Cookie 少了 {attr}：{cookie}"
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(body["access_token"]))).status_code == 200


@pytest.mark.anyio
async def test_refresh_needs_csrf_header_and_rotates(db_session, https_client) -> None:
    u = await _user(db_session)
    await _login(https_client, u)
    assert (await https_client.post("/api/v1/auth/refresh")).status_code == 403
    first = https_client.cookies.get(sessions.REFRESH_COOKIE)
    r = await https_client.post("/api/v1/auth/refresh", headers=H)
    assert r.status_code == 200, r.text
    second = https_client.cookies.get(sessions.REFRESH_COOKIE)
    assert second and second != first, "換發沒有換新的更新權杖"
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(r.json()["access_token"]))).status_code == 200


@pytest.mark.anyio
async def test_reused_refresh_token_revokes_the_session(db_session, https_client) -> None:
    u = await _user(db_session)
    access = await _login(https_client, u)
    stolen = https_client.cookies.get(sessions.REFRESH_COOKIE)
    assert (await https_client.post("/api/v1/auth/refresh", headers=H)).status_code == 200
    # 兩個分頁同時換發：寬限內的舊權杖照樣能用
    assert (await _refresh_with(https_client, stolen)).status_code == 200
    # 超過寬限：把換發時間往前推，再拿最早那一把 → 重用 → 整個工作階段撤銷
    row = (await db_session.execute(select(UserSession).where(UserSession.user_id == u.id))).scalar_one()
    await db_session.refresh(row)
    row.rotated_at = datetime.now(UTC) - timedelta(minutes=5)
    row.prev_refresh_hash = sessions._hash(stolen.split(".", 1)[1])
    await db_session.commit()
    r = await _refresh_with(https_client, stolen)
    assert r.status_code == 401
    await db_session.refresh(row)
    assert row.revoked_at is not None and row.revoked_reason == "refresh_reuse"
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(access))).status_code == 401
    from app.models.audit import AuditLog
    acts = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert "refresh_token_reuse" in acts


@pytest.mark.anyio
async def test_logout_revokes_immediately(db_session, https_client) -> None:
    u = await _user(db_session)
    access = await _login(https_client, u)
    r = await https_client.post("/api/v1/auth/logout", headers={**H, **_bearer(access)})
    assert r.status_code == 204
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(access))).status_code == 401, \
        "登出之後存取權杖還能用（以前登出什麼都不做）"
    assert (await https_client.post("/api/v1/auth/refresh", headers=H)).status_code == 401


@pytest.mark.anyio
async def test_logout_without_csrf_header_is_refused(db_session, https_client) -> None:
    u = await _user(db_session)
    access = await _login(https_client, u)
    assert (await https_client.post("/api/v1/auth/logout", headers=_bearer(access))).status_code == 403


@pytest.mark.anyio
async def test_password_change_revokes_other_sessions_only(db_session) -> None:
    from app.main import create_app
    u = await _user(db_session)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as a, \
            AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as b:
        tok_a = await _login(a, u)
        tok_b = await _login(b, u)
        r = await a.post("/api/v1/auth/change-password", headers=_bearer(tok_a),
                         json={"current_password": PASSWORD, "new_password": "AnotherPassword2026!"})
        assert r.status_code == 204, r.text
        assert (await a.get("/api/v1/auth/me", headers=_bearer(tok_a))).status_code == 200
        assert (await b.get("/api/v1/auth/me", headers=_bearer(tok_b))).status_code == 401


@pytest.mark.anyio
async def test_deactivation_revokes_sessions_and_api_tokens(db_session, https_client) -> None:
    from app.services.auth import issue_access_token
    admin = await _user(db_session, admin=True)
    u = await _user(db_session)
    tok = await _login(https_client, u)
    r = await https_client.post("/api/v1/api-tokens", headers=_bearer(tok), json={"name": "bot"})
    api_tok = r.json()["token"]
    admin_h = _bearer(issue_access_token(admin))
    assert (await https_client.patch(f"/api/v1/users/{u.id}", headers=admin_h, json={"is_active": False})).status_code == 200
    assert (await https_client.patch(f"/api/v1/users/{u.id}", headers=admin_h, json={"is_active": True})).status_code == 200
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(tok))).status_code == 401
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(api_tok))).status_code == 401, \
        "停用再啟用之後舊的 API 權杖又回來了"


@pytest.mark.anyio
async def test_admin_force_logout_also_kills_tokens_without_sid(db_session, https_client) -> None:
    from app.services.auth import issue_access_token
    admin = await _user(db_session, admin=True)
    u = await _user(db_session)
    tok = await _login(https_client, u)
    legacy = issue_access_token(u)        # 不帶 sid（升級前簽發的那種）
    import asyncio
    await asyncio.sleep(1.1)              # iat 以秒為單位：強制登出要晚於簽發
    r = await https_client.post(f"/api/v1/users/{u.id}/revoke-sessions", headers=_bearer(issue_access_token(admin)))
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(tok))).status_code == 401
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(legacy))).status_code == 401
    # 之後重新登入照常
    assert (await https_client.get("/api/v1/auth/me", headers=_bearer(await _login(https_client, u)))).status_code == 200


@pytest.mark.anyio
async def test_list_and_revoke_own_sessions(db_session) -> None:
    from app.main import create_app
    u = await _user(db_session)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as a, \
            AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as b:
        tok_a = await _login(a, u)
        tok_b = await _login(b, u)
        lst = (await a.get("/api/v1/auth/sessions", headers=_bearer(tok_a))).json()
        assert len(lst) == 2 and sum(1 for x in lst if x["current"]) == 1
        other = next(x for x in lst if not x["current"])
        assert (await a.delete(f"/api/v1/auth/sessions/{other['id']}", headers=_bearer(tok_a))).status_code == 204
        assert (await b.get("/api/v1/auth/me", headers=_bearer(tok_b))).status_code == 401
        # 別人的工作階段刪不到（404，不透露存在）
        assert (await b.delete(f"/api/v1/auth/sessions/{uuid.uuid4()}", headers=_bearer(tok_a))).status_code == 404


def test_production_code_always_issues_access_tokens_with_a_session() -> None:
    """不帶 sid 的存取權杖撤銷不了（只受強制登出的時間門檻管）：正式程式一律要帶。"""
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    calls, missing = 0, []
    for p in app_dir.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name == "issue_access_token":
                    calls += 1
                    if not any(k.arg == "sid" for k in node.keywords):
                        missing.append(f"{p.relative_to(app_dir)}:{node.lineno}")
    assert calls >= 2
    assert not missing, f"這些地方簽發的存取權杖沒有工作階段：{missing}"


@pytest.mark.anyio
async def test_sso_handoff_uses_cookie_not_url(db_session) -> None:
    from starlette.requests import Request

    from app.api.v1.endpoints.sso import _sso_finish
    u = await _user(db_session)
    req = Request({"type": "http", "headers": [(b"user-agent", b"pytest")], "client": ("192.0.2.9", 1)})
    resp = await _sso_finish(db_session, u, req, method="oidc", login_url="https://ipam.example/login")
    loc = resp.headers["location"]
    assert loc.endswith("#sso=1") and "token" not in loc, f"網址上不可以有權杖：{loc}"
    assert "jt_refresh=" in resp.headers["set-cookie"]
