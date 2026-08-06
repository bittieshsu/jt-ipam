"""用網卡 MAC 把 IP 掛回它所屬的裝置。

由來：一台雙網卡機器的第二個 IP 常常沒有 `device_id` —— 整合同步只認得第一張網卡，
或那筆 IP 是掃描代理發現後建立的。但那個 MAC 就寫在該裝置的連接埠上，系統其實知道
答案，只是沒去用。畫面上的症狀是裝置頁「IP 清單」少一筆，AI 巡檢還會據此報誤警。

**寧可不掛也不要掛錯**（本專案在整合同步上一貫的原則）：同一個 MAC 對到多台裝置時
不猜，已經有 device_id 的一律不動，也永遠不會把既有的關聯拿掉。
"""
from __future__ import annotations

import uuid

import pytest
from app.services import ip_device_link as link


async def _fixture(db_session, *, second_mac="00:00:5e:00:53:02"):
    from app.models.address import IPAddress
    from app.models.device import Device
    from app.models.physical import DevicePort
    from app.models.section import Section
    from app.models.subnet import Subnet

    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    dev = Device(name=f"srv-{uuid.uuid4().hex[:6]}", type="server")
    db_session.add_all([sub, dev])
    await db_session.flush()
    db_session.add_all([
        DevicePort(device_id=dev.id, name="eth0", mac_address="00:00:5e:00:53:01"),
        DevicePort(device_id=dev.id, name="eth1", mac_address=second_mac),
    ])
    a = IPAddress(subnet_id=sub.id, ip="198.51.100.10", mac="00:00:5e:00:53:01",
                  device_id=dev.id)
    b = IPAddress(subnet_id=sub.id, ip="198.51.100.11", mac="00:00:5e:00:53:02")
    db_session.add_all([a, b])
    await db_session.flush()
    return dev, a, b


@pytest.mark.anyio
async def test_an_unlinked_ip_is_attached_via_its_nic_mac(db_session):
    dev, _a, b = await _fixture(db_session)
    n = await link.link_by_port_mac(db_session)
    await db_session.flush()
    assert n >= 1
    await db_session.refresh(b)
    assert b.device_id == dev.id


@pytest.mark.anyio
async def test_dry_run_changes_nothing(db_session):
    """先看會動到什麼再動 —— 這是會改資料的動作。"""
    _dev, _a, b = await _fixture(db_session)
    n = await link.link_by_port_mac(db_session, dry_run=True)
    await db_session.flush()
    await db_session.refresh(b)
    assert n >= 1 and b.device_id is None


@pytest.mark.anyio
async def test_an_ambiguous_mac_is_left_alone(db_session):
    """同一個 MAC 出現在兩台裝置上就不猜。

    掛錯比沒有更糟：沒有關聯您會去查，掛錯了不會有人發現。
    """
    from app.models.device import Device
    from app.models.physical import DevicePort

    _dev, _a, b = await _fixture(db_session)
    other = Device(name=f"other-{uuid.uuid4().hex[:6]}", type="server")
    db_session.add(other)
    await db_session.flush()
    db_session.add(DevicePort(device_id=other.id, name="eth9",
                              mac_address="00:00:5e:00:53:02"))
    await db_session.flush()

    await link.link_by_port_mac(db_session)
    await db_session.flush()
    await db_session.refresh(b)
    assert b.device_id is None


@pytest.mark.anyio
async def test_an_existing_link_is_never_overwritten(db_session):
    """人工指定過的關聯不能被自動邏輯改掉。"""
    from app.models.device import Device
    from app.models.physical import DevicePort

    dev, a, _b = await _fixture(db_session)
    manual = Device(name=f"manual-{uuid.uuid4().hex[:6]}", type="server")
    db_session.add(manual)
    await db_session.flush()
    a.device_id = manual.id
    db_session.add(DevicePort(device_id=dev.id, name="eth2",
                              mac_address="00:00:5e:00:53:01"))
    await db_session.flush()

    await link.link_by_port_mac(db_session)
    await db_session.flush()
    await db_session.refresh(a)
    assert a.device_id == manual.id


@pytest.mark.anyio
async def test_the_change_is_recorded_in_the_ip_history(db_session):
    """自動掛上的關聯要留痕，否則使用者看到裝置變了卻查不到是誰改的。"""
    from sqlalchemy import select
    from app.models.ip_change_log import IPChangeLog

    _dev, _a, b = await _fixture(db_session)
    await link.link_by_port_mac(db_session)
    await db_session.flush()
    rows = (await db_session.execute(
        select(IPChangeLog).where(IPChangeLog.ip_id == b.id,
                                  IPChangeLog.field == "device_id")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].source != "manual"
