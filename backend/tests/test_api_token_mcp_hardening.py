"""API 權杖與對外 MCP 金鑰的三個缺口（2026-10-09 合規核對）。

1. `object_filters` 欄位可以填、會存，但**沒有任何地方讀它** —— 填了以為權杖被限制在某些物件，
   實際上照樣沿用擁有者的完整權限。看似有用其實沒作用的旋鈕比沒有更危險 → 拿掉（要限制範圍，
   照文件另建低權限帳號再用它建權杖）。
2. `rate_limit_api_token`（每把權杖每分鐘 600 次）定義了卻從沒套用。
3. 對外 MCP 金鑰（jtmcp_）不會過期。改成輪替時選有效天數，到期前通知管理員。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings


@pytest.mark.anyio
async def test_object_filters_is_rejected_and_not_returned(client, auth_headers) -> None:
    r = await client.post("/api/v1/api-tokens", headers=auth_headers,
                          json={"name": "t1", "object_filters": {"subnet": ["x"]}})
    assert r.status_code == 422, "object_filters 沒有作用，送進來要被拒絕，不可以假裝收下"
    r = await client.post("/api/v1/api-tokens", headers=auth_headers, json={"name": "t2"})
    assert r.status_code == 201, r.text
    lst = await client.get("/api/v1/api-tokens", headers=auth_headers)
    assert lst.status_code == 200
    items = lst.json()["items"] if isinstance(lst.json(), dict) else lst.json()
    assert items and all("object_filters" not in it for it in items)


def test_object_filters_column_is_gone() -> None:
    from app.models.user import APIToken
    assert "object_filters" not in APIToken.__table__.columns


@pytest.mark.anyio
async def test_api_token_requests_are_rate_limited(client, auth_headers, monkeypatch) -> None:
    r = await client.post("/api/v1/api-tokens", headers=auth_headers, json={"name": "rl"})
    token = r.json()["token"]
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "rate_limit_api_token", "2/minute")
    h = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 429


@pytest.mark.anyio
async def test_mcp_key_has_expiry_and_expired_key_is_rejected(db_session, admin_user) -> None:
    from app.services.system_config import get_llm_config, mcp_key_valid, rotate_mcp_api_key

    key = await rotate_mcp_api_key(db_session, principal_user_id=admin_user.id, expires_in_days=30)
    cfg = await get_llm_config(db_session)
    assert cfg.mcp_api_key == key
    assert cfg.mcp_api_key_expires_at is not None
    left = cfg.mcp_api_key_expires_at - datetime.now(UTC)
    assert timedelta(days=29) < left <= timedelta(days=30)
    assert mcp_key_valid(cfg, key)
    cfg.mcp_api_key_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not mcp_key_valid(cfg, key), "過期的 MCP 金鑰仍然有效"
    assert not mcp_key_valid(cfg, "jtmcp_wrong")


@pytest.mark.anyio
async def test_rotate_endpoint_takes_days_and_settings_show_expiry(client, auth_headers) -> None:
    r = await client.post("/api/v1/system/llm/mcp-key/rotate", headers=auth_headers, json={"expires_in_days": 7})
    assert r.status_code == 200, r.text
    assert r.json()["api_key"].startswith("jtmcp_")
    exp = datetime.fromisoformat(r.json()["expires_at"])
    assert timedelta(days=6) < exp - datetime.now(UTC) <= timedelta(days=7)
    cfg = await client.get("/api/v1/system/llm", headers=auth_headers)
    assert cfg.json()["mcp_api_key_expires_at"]
    bad = await client.post("/api/v1/system/llm/mcp-key/rotate", headers=auth_headers, json={"expires_in_days": 999})
    assert bad.status_code == 422


def _load_migration():
    import importlib.util
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "alembic/versions/0199_token_expiry_and_cleanup.py"
    spec = importlib.util.spec_from_file_location("m0199", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_upgrade_gives_existing_mcp_key_an_expiry_instead_of_cutting_it_off() -> None:
    """既有金鑰升級後不能立刻失效（外部 MCP 用戶端會突然斷掉），給 90 天並照常通知。"""
    m = _load_migration()
    now = datetime(2026, 10, 9, tzinfo=UTC)
    v = m.with_default_expiry({"mcp_api_key_enc": "v1:xx:yy"}, "mcp_api_key_enc", "mcp_api_key_expires_at", now)
    assert v["mcp_api_key_expires_at"] == (now + timedelta(days=90)).isoformat()
    assert m.with_default_expiry({}, "mcp_api_key_enc", "mcp_api_key_expires_at", now) is None
    has = {"mcp_api_key_enc": "v1:xx:yy", "mcp_api_key_expires_at": "2027-01-01T00:00:00+00:00"}
    assert m.with_default_expiry(has, "mcp_api_key_enc", "mcp_api_key_expires_at", now) is None


@pytest.mark.anyio
async def test_expiring_credentials_notify_once_per_threshold(db_session, admin_user) -> None:
    from sqlalchemy import func, select

    from app.models.notification import Notification
    from app.services.credential_expiry import check_credential_expiry
    from app.services.system_config import rotate_mcp_api_key

    await rotate_mcp_api_key(db_session, principal_user_id=admin_user.id, expires_in_days=5)
    s1 = await check_credential_expiry(db_session)
    await db_session.commit()
    assert s1["notified"] >= 1
    n1 = (await db_session.execute(select(func.count()).select_from(Notification)
                                   .where(Notification.user_id == admin_user.id))).scalar_one()
    s2 = await check_credential_expiry(db_session)     # 同一個門檻（7 天內）再跑一次 → 不重複
    await db_session.commit()
    n2 = (await db_session.execute(select(func.count()).select_from(Notification)
                                   .where(Notification.user_id == admin_user.id))).scalar_one()
    assert n1 == n2 and s2["notified"] == 0


@pytest.mark.anyio
async def test_api_token_owner_is_notified_before_expiry(db_session, admin_user) -> None:
    from sqlalchemy import select

    from app.core.security import hash_api_token
    from app.models.notification import Notification
    from app.models.user import APIToken
    from app.services.credential_expiry import check_credential_expiry

    db_session.add(APIToken(user_id=admin_user.id, name="ci-bot", token_hash=hash_api_token(f"jt_{uuid.uuid4()}"),
                            token_prefix="jt_test", scopes=[], expires_at=datetime.now(UTC) + timedelta(days=2)))
    await db_session.commit()
    await check_credential_expiry(db_session)
    await db_session.commit()
    rows = (await db_session.execute(select(Notification).where(Notification.user_id == admin_user.id))).scalars().all()
    assert any((n.params or {}).get("name") == "ci-bot" for n in rows)
