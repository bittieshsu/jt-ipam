"""雙網卡主機的另一張網卡回應這個 IP（ARP flux）時，不可以把那張網卡的 MAC 寫成這個 IP 的 MAC。

正式環境 2026-10-20（使用者推測、查證屬實）：雙網卡 VM 的 IP 記錄 MAC 在兩張網卡之間來回換 —— 一個 IP 30 天換了 91 次，
IP 異動記錄被洗版，「裝置關聯可能過期」整頁都是這些。成因：Linux 預設 arp_ignore=0，兩個網段在同一個二層時，
兩張網卡都回答；掃描代理／防火牆看到哪張就寫哪張。

規則（全部成立才不換）：新的 MAC 登記在另一個子網路的 IP 上（那是那張網卡自己的位址）、目前的 MAC 只屬於這個 IP、
目前的 MAC 24 小時內還在這個 IP 被看到。真的換網卡、搬 VLAN（舊的 MAC 一天沒出現）照常換。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.librenms import ARPEntry
from app.services.arp_precedence import consider_mac
from tests.test_ip_conflict_evidence import _ip, _subnet

pytestmark = pytest.mark.anyio

OWN, OTHER = "bc:24:11:5e:01:01", "bc:24:11:5e:02:02"


async def _host(db, *, seen_hours_ago: float = 0.2):  # type: ignore[no-untyped-def]
    s1 = await _subnet(db, "198.51.100.0/24")
    s2 = await _subnet(db, "203.0.113.0/24")
    a = await _ip(db, s1, "198.51.100.45", mac=OWN)
    b = await _ip(db, s2, "203.0.113.45", mac=OTHER)
    a.mac_source = b.mac_source = "scanner"
    db.add(ARPEntry(ip="198.51.100.45", mac=OWN, source="scanner",
                    first_seen_at=datetime.now(UTC) - timedelta(days=30),
                    last_seen_at=datetime.now(UTC) - timedelta(hours=seen_hours_ago)))
    await db.flush()
    return a, b


async def test_the_other_nics_mac_is_not_taken(db_session) -> None:
    a, _b = await _host(db_session)
    assert await consider_mac(db_session, ip=a, mac=OTHER, source="scanner") is False
    assert str(a.mac) == OWN


async def test_a_real_nic_change_still_happens(db_session) -> None:
    """舊的 MAC 一天以上沒在這個 IP 出現：換了網卡或搬了 VLAN，照常換。"""
    a, _b = await _host(db_session, seen_hours_ago=30)
    assert await consider_mac(db_session, ip=a, mac=OTHER, source="scanner") is True
    assert str(a.mac) == OTHER


async def test_a_record_already_holding_the_other_nics_mac_heals(db_session) -> None:
    """已經被寫錯（兩筆記錄掛同一個 MAC）：看到自己的 MAC 就換回來，不卡住。"""
    a, b = await _host(db_session)
    a.mac = OTHER
    await db_session.flush()
    assert await consider_mac(db_session, ip=a, mac=OWN, source="scanner") is True
    assert str(a.mac) == OWN
    assert str(b.mac) == OTHER


async def test_same_subnet_aliases_are_not_blocked(db_session) -> None:
    """同一個子網路的兩個位址共用 MAC（別名、VIP）不是 ARP flux。"""
    s1 = await _subnet(db_session, "198.51.100.0/24")
    a = await _ip(db_session, s1, "198.51.100.46", mac=OWN)
    await _ip(db_session, s1, "198.51.100.47", mac=OTHER)
    a.mac_source = "scanner"
    db_session.add(ARPEntry(ip="198.51.100.46", mac=OWN, source="scanner", last_seen_at=datetime.now(UTC)))
    await db_session.flush()
    assert await consider_mac(db_session, ip=a, mac=OTHER, source="scanner") is True
