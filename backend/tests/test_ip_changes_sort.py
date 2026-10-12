"""IP 異動記錄頁的欄位標題要能排序（使用者 2026-10-12）。

這頁是伺服器端分頁，排序在伺服器做。IP 照位址大小排（10.0.0.2 在 10.0.0.10 前面），沒有值的排最後；
不認得的排序鍵退回預設（時間新的在前）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.ip_change_log import IPChangeLog
from app.models.section import Section
from app.models.subnet import Subnet

pytestmark = pytest.mark.anyio


@pytest.fixture
async def logs(db_session, admin_user):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="10.0.0.0/24")
    db_session.add(sub)
    await db_session.flush()
    db_session.add_all([
        IPChangeLog(subnet_id=sub.id, ip_text="10.0.0.10", event_type="online", field="effective_status",
                    source="scanner", created_at=now - timedelta(minutes=1)),
        IPChangeLog(subnet_id=sub.id, ip_text="10.0.0.2", event_type="edited", field="note",
                    source="manual", actor_user_id=admin_user.id, created_at=now - timedelta(minutes=3)),
        IPChangeLog(subnet_id=sub.id, ip_text="10.0.0.9", event_type="mac_changed", field="mac",
                    source="librenms", created_at=now - timedelta(minutes=2)),
    ])
    await db_session.commit()
    return sub


async def _ips(client, auth_headers, sub, **params):  # type: ignore[no-untyped-def]
    r = await client.get("/api/v1/ip-changes", headers=auth_headers,
                         params={"subnet_id": str(sub.id), "page_size": 50, **params})
    assert r.status_code == 200, r.text
    return [x["ip_text"].rsplit(".", 1)[1] for x in r.json()["items"]]


async def test_default_is_newest_first(client, auth_headers, logs) -> None:
    assert await _ips(client, auth_headers, logs) == ["10", "9", "2"]
    assert await _ips(client, auth_headers, logs, sort="nonsense", order="asc") == ["10", "9", "2"]


@pytest.mark.parametrize(("key", "asc"), [
    ("created_at", ["2", "9", "10"]),
    ("ip", ["2", "9", "10"]),                 # 照位址大小，不是字串
    ("event_type", ["2", "9", "10"]),         # edited < mac_changed < online
    ("field", ["10", "9", "2"]),              # effective_status < mac < note
    ("source", ["9", "2", "10"]),             # librenms < manual < scanner
])
async def test_columns_sort_both_ways(client, auth_headers, logs, key, asc) -> None:
    assert await _ips(client, auth_headers, logs, sort=key, order="asc") == asc
    assert await _ips(client, auth_headers, logs, sort=key, order="desc") == asc[::-1]


async def test_actor_sort_puts_rows_without_an_actor_last(client, auth_headers, logs) -> None:
    for direction in ("asc", "desc"):
        got = await _ips(client, auth_headers, logs, sort="actor", order=direction)
        assert got[0] == "2", "有操作者的在前，沒有的（系統、整合）排最後"
