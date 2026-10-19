"""儀表板「近 14 日 稽核 / IP 異動 趨勢」可以排除子網路、DHCP 範圍、異動類型（使用者 2026-10-18）。

IP 異動的量大多是 DHCP 範圍裡的機器上線／離線與 MAC 變化，真正在意的變化被淹掉；設定跟著帳號存（user_preferences.dashboard）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models.ip_change_log import EVENT_TYPES, IPChangeLog
from app.models.ip_range import IPRange
from app.models.section import Section
from app.models.subnet import Subnet

pytestmark = pytest.mark.anyio


async def _site(db):  # type: ignore[no-untyped-def]
    sec = Section(name=f"tr-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    a = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    b = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    db.add_all([a, b])
    await db.flush()
    db.add(IPRange(subnet_id=a.id, start_ip="198.51.100.100", end_ip="198.51.100.199", purpose="dhcp"))
    now = datetime.now(UTC)
    for ip_text, sub, ev in [
        ("198.51.100.150", a, "online"),          # DHCP 範圍裡、上線
        ("198.51.100.151", a, "mac_changed"),     # DHCP 範圍裡、MAC
        ("198.51.100.10", a, "hostname_changed"),
        ("198.51.100.11", a, "edited"),
        ("203.0.113.5", b, "offline"),
    ]:
        db.add(IPChangeLog(subnet_id=sub.id, ip_text=ip_text, event_type=ev, source="scanner", created_at=now))
    await db.commit()
    return a, b


def _total(body) -> int:  # type: ignore[no-untyped-def]
    return sum(p["ip_changes"] for p in body["activity_trend"])


async def _set(client, headers, trend) -> None:  # type: ignore[no-untyped-def]
    r = await client.patch("/api/v1/me/preferences", json={"dashboard": {"trend": trend}}, headers=headers)
    assert r.status_code == 200, r.text


async def test_no_settings_counts_everything(client, auth_headers, db_session) -> None:
    await _site(db_session)
    r = await client.get("/api/v1/dashboard/overview", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert _total(r.json()) == 5
    assert r.json()["activity_trend_filter"]["active"] is False


async def test_exclude_subnets_dhcp_ranges_and_event_types(client, auth_headers, db_session) -> None:
    a, b = await _site(db_session)
    await _set(client, auth_headers, {"exclude_subnet_ids": [str(b.id)]})
    body = (await client.get("/api/v1/dashboard/overview", headers=auth_headers)).json()
    assert _total(body) == 4
    assert body["activity_trend_filter"] == {"active": True, "excluded_subnets": 1, "exclude_dhcp": False,
                                             "event_groups": None}

    await _set(client, auth_headers, {"exclude_subnet_ids": [str(b.id)], "exclude_dhcp": True})
    assert _total((await client.get("/api/v1/dashboard/overview", headers=auth_headers)).json()) == 2

    await _set(client, auth_headers, {"event_groups": ["hostname", "edited"]})
    assert _total((await client.get("/api/v1/dashboard/overview", headers=auth_headers)).json()) == 2

    r = await client.get("/api/v1/me/preferences", headers=auth_headers)
    assert r.json()["dashboard"]["trend"]["event_groups"] == ["hostname", "edited"]


async def test_unknown_event_group_is_rejected(client, auth_headers) -> None:
    r = await client.patch("/api/v1/me/preferences", json={"dashboard": {"trend": {"event_groups": ["nope"]}}},
                           headers=auth_headers)
    assert r.status_code == 422


def test_every_event_type_belongs_to_a_group() -> None:
    """新增 IP 異動事件類型時要歸到一組，否則勾了「全部」也篩不到它。"""
    from app.api.v1.endpoints.dashboard import TREND_EVENT_GROUPS
    grouped = {e for evs in TREND_EVENT_GROUPS.values() for e in evs}
    assert set(EVENT_TYPES) == grouped, set(EVENT_TYPES) ^ grouped

