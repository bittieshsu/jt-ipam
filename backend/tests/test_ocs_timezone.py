"""issue #51：OCS 的 LASTDATE 是 OCS 伺服器的本地時間（MySQL DATETIME，沒有時區），不一定是 UTC。

以前固定當成 UTC：時區是 UTC+8 的 OCS，最後盤點時間會超前 8 小時、甚至落在未來，
「盤點太舊」的判斷也跟著偏。現在每個 OCS 整合有自己的時區：
- 新建的整合預設用 jt-ipam 主機的時區（APP_TIMEZONE）
- 升級前就有的整合是 UTC（migration 0206；不改變既有結果），換算後落在未來時同步結果會提醒時區可能設錯
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import app.services.ocs as ocs
import pytest
from app.core.config import get_settings


def test_lastdate_uses_the_ocs_timezone() -> None:
    hw = {"LASTDATE": "2026-10-11 13:25:24"}
    assert ocs.lastdate_of(hw, "Asia/Taipei") == datetime(2026, 10, 11, 5, 25, 24, tzinfo=UTC)
    assert ocs.lastdate_of(hw, "UTC") == datetime(2026, 10, 11, 13, 25, 24, tzinfo=UTC)
    assert ocs.lastdate_of(hw) == datetime(2026, 10, 11, 13, 25, 24, tzinfo=UTC), "沒給時區＝UTC（舊行為）"
    assert ocs.lastdate_of(hw, "America/New_York") == datetime(2026, 10, 11, 17, 25, 24, tzinfo=UTC)
    assert ocs.lastdate_of({"LASTDATE": ""}, "Asia/Taipei") is None


@pytest.mark.anyio
async def test_api_timezone_default_validation_and_update(client, auth_headers) -> None:
    r = await client.post("/api/v1/ocs", headers=auth_headers,
                          json={"name": f"ocs-tz-{uuid.uuid4().hex[:6]}", "base_url": "https://192.0.2.12"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["timezone"] == get_settings().app_timezone, "新建的整合預設用 jt-ipam 主機的時區"
    sid = body["id"]

    r = await client.patch(f"/api/v1/ocs/{sid}", headers=auth_headers, json={"timezone": "UTC"})
    assert r.status_code == 200
    assert r.json()["timezone"] == "UTC"
    r = await client.patch(f"/api/v1/ocs/{sid}", headers=auth_headers, json={"timezone": "Mars/Olympus"})
    assert r.status_code == 422, "認不得的時區要擋下來，不能存進去之後每次同步才爆"
    r = await client.post("/api/v1/ocs", headers=auth_headers, json={
        "name": f"ocs-tz-{uuid.uuid4().hex[:6]}", "base_url": "https://192.0.2.13", "timezone": "Asia/Tokyo"})
    assert r.status_code == 201
    assert r.json()["timezone"] == "Asia/Tokyo"


class _Resp:
    def __init__(self, status: int, data: object) -> None:
        self.status_code = status
        self._data = data

    def json(self) -> object:
        return self._data

    @property
    def content(self) -> bytes:
        import json as _json
        return _json.dumps(self._data).encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


async def _sync_one(monkeypatch, db_session, *, tz: str, lastdate: str, mac: str, ip_str: str):  # type: ignore[no-untyped-def]
    from app.models.address import IPAddress
    from app.models.section import Section
    from app.models.subnet import Subnet

    sec = Section(name=f"ocstz-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    ip = IPAddress(subnet_id=sub.id, ip=ip_str, mac=mac)
    db_session.add(ip)
    await db_session.flush()
    page = {"1": {"hardware": {"NAME": "pc-1", "LASTDATE": lastdate},
                  "networks": [{"MACADDR": mac, "VIRTUALDEV": 0}]}}

    class _C:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False

    async def fake_request(method, url, **kw):  # type: ignore[no-untyped-def]
        if "lastupdate" in url:
            return _Resp(404, None)
        return _Resp(200, page if "start=0" in url else {})

    monkeypatch.setattr(ocs, "safe_client", lambda *a, **k: _C())
    monkeypatch.setattr(ocs, "safe_request", fake_request)
    srv = SimpleNamespace(
        id=uuid.uuid4(), name="ocs", source_type="rest", base_url="https://192.0.2.10", verify_tls=False,
        api_username=None, api_password_enc=None, api_password_nonce=None, sync_bios=False,
        stale_after_days=30, last_incremental_epoch=None, detected_version=None, last_sync_at=None,
        last_success_at=None, last_error=None, last_cost=None, scope_subnet_ids=None, timezone=tz)
    summary = await ocs.sync_instance(db_session, srv)
    await db_session.flush()
    await db_session.refresh(ip)
    return ip, summary


@pytest.mark.anyio
async def test_sync_converts_with_the_ocs_timezone(monkeypatch, db_session) -> None:
    local = datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(minutes=10)
    ip, summary = await _sync_one(monkeypatch, db_session, tz="Asia/Taipei",
                                  lastdate=local.strftime("%Y-%m-%d %H:%M:%S"),
                                  mac="02:00:5e:00:53:51", ip_str="198.51.100.51")
    assert ip.last_seen_ocs is not None
    assert abs((ip.last_seen_ocs - local.astimezone(UTC)).total_seconds()) < 2
    assert "future_lastdate" not in summary


@pytest.mark.anyio
async def test_sync_flags_lastdate_in_the_future(monkeypatch, db_session) -> None:
    """設成 UTC、OCS 其實是 UTC+8：剛盤點的電腦換算後超前 8 小時 → 同步結果提醒時區可能設錯。"""
    local = datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(minutes=5)
    _ip, summary = await _sync_one(monkeypatch, db_session, tz="UTC",
                                   lastdate=local.strftime("%Y-%m-%d %H:%M:%S"),
                                   mac="02:00:5e:00:53:52", ip_str="198.51.100.52")
    flag = summary.get("future_lastdate")
    assert flag == {"count": 1, "max_hours": 8, "timezone": "UTC"}
