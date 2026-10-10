"""公開端點（Graylog DSV、機櫃嵌入圖）的權杖與明文埠（2026-10-09 合規核對）。

以前：兩把權杖明文存在設定裡、永不過期、設定頁每次載入都整把回給瀏覽器；nginx 預設開著明文
8088 埠給 DSV，權杖在網址上。現在：加密存放、會到期、要按「顯示」（留稽核）；明文 8088 要在
設定頁明確打開；可以限制來源網段；也接受 `X-Auth-Token` 標頭。
"""
from __future__ import annotations

import importlib.util
import inspect
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.services.system_config import get_graylog_dsv, set_graylog_dsv


@pytest.fixture
async def https_client():
    from app.main import create_app
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as c:
        yield c


async def _dsv(db_session, **kw):
    return await set_graylog_dsv(db_session, enabled=True, fmt="csv", path="ip-fqdn",
                                 regenerate_token=True, **kw)


@pytest.mark.anyio
async def test_plain_http_is_off_unless_turned_on(db_session, client, https_client) -> None:
    cfg = await _dsv(db_session)
    tok = cfg["token"]
    assert (await client.get(f"/api/v1/lookup/ip-fqdn?token={tok}")).status_code == 404, \
        "明文 HTTP（8088）沒有打開也照樣服務"
    assert (await https_client.get(f"/api/v1/lookup/ip-fqdn?token={tok}")).status_code == 200
    await set_graylog_dsv(db_session, enabled=True, fmt="csv", path="ip-fqdn", allow_plain_http=True)
    assert (await client.get(f"/api/v1/lookup/ip-fqdn?token={tok}")).status_code == 200


@pytest.mark.anyio
async def test_token_in_header_and_expiry(db_session, https_client) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.system_setting import SystemSetting
    tok = (await _dsv(db_session))["token"]
    assert (await https_client.get("/api/v1/lookup/ip-fqdn", headers={"X-Auth-Token": tok})).status_code == 200
    row = await db_session.get(SystemSetting, "graylog_dsv")
    row.value = {**row.value, "token_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    flag_modified(row, "value")
    await db_session.commit()
    r = await https_client.get(f"/api/v1/lookup/ip-fqdn?token={tok}")
    assert r.status_code == 401 and r.json()["detail"] == "Token expired"


@pytest.mark.anyio
async def test_allowed_sources(db_session, https_client) -> None:
    tok = (await _dsv(db_session, allowed_sources=["192.0.2.0/24"]))["token"]
    assert (await https_client.get(f"/api/v1/lookup/ip-fqdn?token={tok}")).status_code == 404
    await set_graylog_dsv(db_session, enabled=True, fmt="csv", path="ip-fqdn", allowed_sources=["127.0.0.0/8"])
    assert (await https_client.get(f"/api/v1/lookup/ip-fqdn?token={tok}")).status_code == 200
    with pytest.raises(ValueError):
        await set_graylog_dsv(db_session, enabled=True, fmt="csv", path="ip-fqdn", allowed_sources=["not-a-net"])


@pytest.mark.anyio
async def test_settings_page_does_not_return_the_token_and_reveal_is_audited(db_session, client, auth_headers) -> None:
    await _dsv(db_session)
    r = await client.get("/api/v1/system/graylog-dsv", headers=auth_headers)
    assert r.status_code == 200 and "token" not in r.json() and r.json()["token_set"] is True
    assert r.json()["token_expires_at"]
    r = await client.get("/api/v1/system/graylog-dsv/token", headers=auth_headers)
    assert r.status_code == 200 and r.json()["token"]
    r = await client.get("/api/v1/system/rack-embed", headers=auth_headers)
    assert "token" not in r.json()
    from app.models.audit import AuditLog
    diffs = (await db_session.execute(select(AuditLog.diff).where(AuditLog.action == "secret_view"))).scalars().all()
    assert {"setting": "graylog_dsv_token"} in diffs


@pytest.mark.anyio
async def test_tokens_are_encrypted_at_rest(db_session) -> None:
    from app.models.system_setting import SystemSetting
    tok = (await _dsv(db_session))["token"]
    row = await db_session.get(SystemSetting, "graylog_dsv")
    assert "token" not in row.value and tok not in str(row.value), "權杖仍然明文存在設定裡"
    assert (await get_graylog_dsv(db_session))["token"] == tok


@pytest.mark.anyio
async def test_rack_embed_token_expires(db_session, https_client) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.system_setting import SystemSetting
    from app.services.system_config import set_rack_embed
    cfg = await set_rack_embed(db_session, enabled=True, regenerate_token=True, token_days=30)
    await db_session.commit()
    left = cfg["token_expires_at"] - datetime.now(UTC)
    assert timedelta(days=29) < left <= timedelta(days=30)
    row = await db_session.get(SystemSetting, "rack_embed")
    row.value = {**row.value, "token_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    flag_modified(row, "value")
    await db_session.commit()
    import uuid
    r = await https_client.get(f"/api/v1/racks/{uuid.uuid4()}/embed.svg?token={cfg['token']}")
    assert r.status_code == 401 and r.json()["detail"] == "Token expired"


def test_every_dsv_endpoint_goes_through_the_shared_guard() -> None:
    from app.api.v1.endpoints import graylog_dsv as mod
    helpers = {"_fw_dsv_guard", "_pfsense_dsv_guard", "_proxmox_vms_pairs"}
    for route in mod.public_router.routes:
        src = inspect.getsource(route.endpoint)
        assert "_authorize(" in src or any(h + "(" in src for h in helpers), route.path
    for h in helpers:
        assert "_authorize(" in inspect.getsource(getattr(mod, h)), h


def test_migration_converts_and_keeps_existing_8088_users() -> None:
    p = pathlib.Path(__file__).resolve().parents[1] / "alembic/versions/0202_public_endpoint_tokens.py"
    spec = importlib.util.spec_from_file_location("m0202", p)
    m = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(m)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    out = m.convert("graylog_dsv", {"enabled": True, "token": "abc", "fmt": "csv"}, b"setting:graylog_dsv:token", now)
    assert "token" not in out and out["token_enc"].startswith("v1:")
    assert out["token_expires_at"] == (now + timedelta(days=365)).isoformat()
    assert out["allow_plain_http"] is True, "已經在用 DSV 的站台升級後不能突然被切斷"
    off = m.convert("graylog_dsv", {"enabled": False, "token": "abc"}, b"setting:graylog_dsv:token", now)
    assert off["allow_plain_http"] is False
    assert m.convert("rack_embed", out | {"allow_plain_http": True}, b"x", now) is None
