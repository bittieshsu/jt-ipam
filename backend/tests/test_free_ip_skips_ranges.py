"""自動配發要跳過用途是「保留」或「DHCP」的位址範圍（GitHub issue #56）。

回報：範圍 .224–.230 設成保留給 Kubernetes MetalLB 的 VIP，範圍裡還沒有 IP 記錄的 .227–.230 仍被「配發下一個可用 IP」、
IP 申請核准、AI 工具 find_free_ips 配出去；DHCP 範圍也一樣，配成固定 IP 之後跟租約衝突。原因是兩個找空位的函式只排除
已有記錄與冷卻中的位址，`ip_ranges` 完全沒參考。用途「其他」的範圍不擋（只是標註）。自動收錄（掃描、ARP）不受影響。
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.anyio


async def _subnet(db, cidr: str = "198.51.100.0/24"):  # type: ignore[no-untyped-def]
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr=cidr)
    db.add(sub)
    await db.flush()
    return sub


def _range(sub, start: str, end: str, purpose: str):  # type: ignore[no-untyped-def]
    from app.models.ip_range import IPRange
    return IPRange(subnet_id=sub.id, start_ip=start, end_ip=end, purpose=purpose, name=f"r-{purpose}")


async def test_first_free_skips_reserved_and_dhcp_ranges(db_session) -> None:
    from app.services.subnet import find_first_free_address
    sub = await _subnet(db_session, "198.51.100.0/28")       # .1–.14
    db_session.add_all([_range(sub, "198.51.100.1", "198.51.100.4", "dhcp"),
                        _range(sub, "198.51.100.5", "198.51.100.6", "reserved"),
                        _range(sub, "198.51.100.7", "198.51.100.8", "other")])
    await db_session.flush()
    assert await find_first_free_address(db_session, sub) == "198.51.100.7"   # 「其他」只是標註，可以配


async def test_find_free_addresses_skips_ranges(db_session) -> None:
    from app.models.address import IPAddress
    from app.services.subnet import find_free_addresses
    sub = await _subnet(db_session)
    db_session.add_all([_range(sub, "198.51.100.224", "198.51.100.230", "reserved"),
                        _range(sub, "198.51.100.2", "198.51.100.200", "dhcp")])
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.1", state="active"))
    await db_session.flush()
    got = await find_free_addresses(db_session, sub, count=25)
    assert got[:2] == ["198.51.100.201", "198.51.100.202"]
    assert not {f"198.51.100.{i}" for i in range(224, 231)} & set(got)        # MetalLB 的保留段一個都不給
    assert "198.51.100.231" in got
    run = await find_free_addresses(db_session, sub, count=4, consecutive=True)
    assert run == ["198.51.100.201", "198.51.100.202", "198.51.100.203", "198.51.100.204"]


async def test_consecutive_run_does_not_cross_a_reserved_range(db_session) -> None:
    from app.services.subnet import find_free_addresses
    sub = await _subnet(db_session, "198.51.100.0/28")
    db_session.add(_range(sub, "198.51.100.4", "198.51.100.4", "reserved"))
    await db_session.flush()
    assert await find_free_addresses(db_session, sub, count=5, consecutive=True) == [
        "198.51.100.5", "198.51.100.6", "198.51.100.7", "198.51.100.8", "198.51.100.9"]


async def test_full_when_the_rest_is_reserved(db_session) -> None:
    from app.services.subnet import find_first_free_address
    sub = await _subnet(db_session, "198.51.100.0/29")       # .1–.6
    db_session.add(_range(sub, "198.51.100.1", "198.51.100.6", "dhcp"))
    await db_session.flush()
    assert await find_first_free_address(db_session, sub) is None


async def test_ipv6_first_free_skips_ranges(db_session) -> None:
    from app.services.subnet import find_first_free_address
    sub = await _subnet(db_session, "2001:db8:56::/64")
    db_session.add(_range(sub, "2001:db8:56::1", "2001:db8:56::10", "reserved"))
    await db_session.flush()
    assert await find_first_free_address(db_session, sub) == "2001:db8:56::11"


async def test_next_free_api_skips_ranges(client, auth_headers, db_session) -> None:
    sub = await _subnet(db_session, "198.51.100.0/28")
    db_session.add(_range(sub, "198.51.100.1", "198.51.100.3", "reserved"))
    await db_session.commit()
    r = await client.get(f"/api/v1/subnets/{sub.id}/first_free_address", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert "198.51.100.4" in r.text
