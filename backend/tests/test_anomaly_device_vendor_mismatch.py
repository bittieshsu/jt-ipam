"""「裝置關聯可能過期」：LibreNMS 裝置已下線、IP 上的 MAC 廠牌又對不上（使用者 2026-10-14：要加）。

事故：一個位址關聯到一台 UniFi 基地台，但那個位址早就是一台電視在用。MAC 從頭到尾沒變，
原本「關聯之後 MAC 換過」的規則抓不到。新的依據：
- 關聯的裝置在 LibreNMS 是 down 或停用（裝置不在了）
- 裝置是網路設備類廠牌（這類設備的 MAC 前綴就是自家的），而 IP 目前的 MAC 廠牌不是它
排除：LibreNMS 還在線上的、廠牌對得上的、認不得裝置廠牌的、本地管理／隨機的 MAC、VRRP 虛擬 MAC
"""
from __future__ import annotations

import uuid

import pytest
from app.models.address import IPAddress
from app.models.device import Device
from app.models.librenms import LibreNMSDevice, LibreNMSInstance
from app.models.oui import OUIVendor
from app.models.section import Section
from app.models.subnet import Subnet
from app.services.anomaly import detect_stale_device_links

pytestmark = pytest.mark.anyio

DOC_MAC = "00:00:5e:00:53:90"       # RFC 7042 文件用範圍


async def _setup(db, *, mac: str = DOC_MAC, oui_name: str = "Samsung Electronics Co.,Ltd", oui_short: str = "SamsungElect",
                 dev_vendor: str = "unifi", dev_model: str = "UAP-AC-InWall", status: str = "disabled"):  # type: ignore[no-untyped-def]
    db.add(OUIVendor(prefix="00005E", short_name=oui_short, name=oui_name, source="manual"))
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    dev = Device(name=f"ap-{uuid.uuid4().hex[:4]}", type="ap", vendor=dev_vendor, model=dev_model)
    inst = LibreNMSInstance(name=f"lnms-{uuid.uuid4().hex[:6]}", api_url="https://librenms.example",
                            api_token_enc=b"x", api_token_nonce=b"y")
    db.add_all([sub, dev, inst])
    await db.flush()
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.190", mac=mac, hostname="tv-01", state="used",
                   device_id=dev.id)
    db.add(ip)
    db.add(LibreNMSDevice(instance_id=inst.id, legacy_device_id=65, hostname="198.51.100.190", sysname=dev.name,
                          status=status, os=dev_vendor, hardware=dev_model, jt_ipam_device_id=dev.id))
    await db.flush()
    return ip, dev


async def _rows(db, ip):  # type: ignore[no-untyped-def]
    return [r for r in await detect_stale_device_links(db) if r["ip_id"] == str(ip.id)]


async def test_disabled_ap_on_an_address_now_used_by_a_tv_is_flagged(db_session) -> None:
    ip, dev = await _setup(db_session)
    rows = await _rows(db_session, ip)
    assert len(rows) == 1
    r = rows[0]
    assert r["device"] == dev.name
    assert r["reason_key"] == "anomaly.stale_reason_vendor_disabled"
    assert r["reason_params"]["mac_vendor"] == "SamsungElect"
    assert r["reason_params"]["device_vendor"] == "Ubiquiti"


async def test_down_device_also_counts(db_session) -> None:
    ip, _dev = await _setup(db_session, status="down")
    rows = await _rows(db_session, ip)
    assert rows
    assert rows[0]["reason_key"] == "anomaly.stale_reason_vendor_down"


async def test_device_still_up_in_librenms_is_not_flagged(db_session) -> None:
    ip, _dev = await _setup(db_session, status="up")
    assert await _rows(db_session, ip) == []


async def test_matching_vendor_is_not_flagged(db_session) -> None:
    ip, _dev = await _setup(db_session, oui_name="Ubiquiti Inc", oui_short="Ubiquiti")
    assert await _rows(db_session, ip) == []


async def test_unknown_device_vendor_is_not_flagged(db_session) -> None:
    """一般伺服器的網卡常是 Intel／Broadcom：認不得的廠牌不比。"""
    ip, _dev = await _setup(db_session, dev_vendor="linux", dev_model="PowerEdge R650")
    assert await _rows(db_session, ip) == []


async def test_local_and_vrrp_macs_are_not_compared(db_session) -> None:
    ip, _dev = await _setup(db_session, mac="02:00:00:00:00:90")
    assert await _rows(db_session, ip) == []


async def test_vrrp_mac_is_not_compared(db_session) -> None:
    ip, _dev = await _setup(db_session, mac="00:00:5e:00:01:0a", oui_name="ICANN, IANA Department", oui_short="IANA")
    assert await _rows(db_session, ip) == []


async def test_both_reasons_on_one_address_become_one_row(db_session) -> None:
    """關聯之後 MAC 換過、而且廠牌也對不上 → 同一個位址只列一筆，依據寫兩個。"""
    from datetime import UTC, datetime, timedelta

    from app.models.ip_change_log import IPChangeLog
    ip, dev = await _setup(db_session, status="down")
    now = datetime.now(UTC)
    db_session.add_all([
        IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), event_type="updated", field="device_id",
                    old_value=None, new_value=str(dev.id), source="manual", created_at=now - timedelta(days=10)),
        IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), event_type="arp_changed", field="mac",
                    old_value="00:00:5e:00:53:01", new_value=DOC_MAC, source="librenms",
                    created_at=now - timedelta(days=1)),
    ])
    await db_session.flush()
    rows = await _rows(db_session, ip)
    assert len(rows) == 1
    assert rows[0]["reason_key"] == "anomaly.stale_reason_mac_vendor_down"
    assert rows[0]["mac_changed_at"]


def test_short_device_words_match_whole_words_only() -> None:
    from app.services.anomaly import _device_families
    assert [f for f, _ in _device_families("Dell", "BIOS 2.1")] == []
    assert [f for f, _ in _device_families(None, None, "ios", "C9300")] == ["Cisco"]
    assert [f for f, _ in _device_families("TP-Link", "EAP245")] == ["TP-Link"]
