"""「下一個可用位址」不可以給冷卻中的位址（2026-10-13，寫全站搜尋 e2e 時發現）。

位址被刪掉（釋放）後進冷卻期：外面的 DNS 快取、防火牆規則可能還指著上一台。新增 IP 的 API 會擋（409
ip_in_cooldown），但「下一個可用位址」只排除已有記錄的位址：
- 畫面與 phpIPAM 相容 API 的「下一個可用」給出一個建不起來的位址
- 「配發下一個可用」、IP 申請核准、AI 工具的配發直接呼叫建立的服務、不經過那道檢查 —— 會把冷卻中的位址配出去，
  等於繞過冷卻期
一次找多個空位（find_free_addresses）原本就有排除，這裡補上同一條規則。解除冷卻與已過期的不算。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.anyio


async def _subnet(db_session, cidr="198.51.100.0/29"):  # type: ignore[no-untyped-def]
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr=cidr)
    db_session.add(sub)
    await db_session.flush()
    return sub


def _cool(sub, ip, **kw):  # type: ignore[no-untyped-def]
    from app.models.ip_cooldown import IPCooldown
    now = datetime.now(UTC)
    return IPCooldown(subnet_id=sub.id, ip=ip, released_at=now - timedelta(hours=1),
                      until=kw.pop("until", now + timedelta(days=30)), **kw)


async def test_first_free_skips_addresses_in_cooldown(db_session) -> None:
    from app.services.subnet import find_first_free_address
    sub = await _subnet(db_session)
    db_session.add(_cool(sub, "198.51.100.1"))
    db_session.add(_cool(sub, "198.51.100.2"))
    await db_session.flush()
    assert await find_first_free_address(db_session, sub) == "198.51.100.3"


async def test_cleared_or_expired_cooldowns_do_not_count(db_session) -> None:
    from app.services.subnet import find_first_free_address
    sub = await _subnet(db_session)
    db_session.add(_cool(sub, "198.51.100.1", cleared_at=datetime.now(UTC)))
    db_session.add(_cool(sub, "198.51.100.2", until=datetime.now(UTC) - timedelta(minutes=1)))
    await db_session.flush()
    assert await find_first_free_address(db_session, sub) == "198.51.100.1"


async def test_full_when_every_free_address_is_cooling(db_session) -> None:
    from app.models.address import IPAddress
    from app.services.subnet import find_first_free_address
    sub = await _subnet(db_session, "198.51.100.0/30")      # .1 .2 可用
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.1"))
    db_session.add(_cool(sub, "198.51.100.2"))
    await db_session.flush()
    assert await find_first_free_address(db_session, sub) is None


async def test_allocate_next_free_never_hands_out_a_cooling_address(client, auth_headers, db_session) -> None:
    """「配發下一個可用」不經過新增 IP 那道冷卻檢查 —— 以前會把冷卻中的位址直接配出去。"""
    sub = await _subnet(db_session)
    db_session.add(_cool(sub, "198.51.100.1"))
    await db_session.commit()
    r = await client.get(f"/api/v1/subnets/{sub.id}/first_free_address", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["ip"] == "198.51.100.2"


async def test_address_range_next_free_skips_cooling_addresses(db_session) -> None:
    """位址範圍清單的「下一個可用」按下去是新增 IP —— 給冷卻中的位址就會被擋（409）。"""
    from app.models.ip_range import IPRange
    from app.services.ip_ranges import ranges_with_usage
    sub = await _subnet(db_session)
    db_session.add(IPRange(subnet_id=sub.id, start_ip="198.51.100.1", end_ip="198.51.100.5", purpose="dhcp"))
    db_session.add(_cool(sub, "198.51.100.1"))
    await db_session.flush()
    (row,) = await ranges_with_usage(db_session, sub)
    assert row["first_free"] == "198.51.100.2"
    assert row["used"] == 0, "冷卻中不是「已用」（沒有 IP 記錄），只是不能馬上配"
