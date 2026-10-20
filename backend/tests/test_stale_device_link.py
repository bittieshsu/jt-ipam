"""IP 掛著某台裝置，但那個位址後來換了 MAC → 關聯多半已經過期。

由來（使用者提問，2026-08-30）：「日後 IP 如果被別的主機用，這邊還是會掛在這個裝置上嗎？」
會 —— 關聯一旦寫下去就不再重新評估，位址被別台機器拿去用（DHCP 尤其常見）之後，
關聯會**安靜地變成錯的**：畫面看起來一切正常，只是指到了另一台機器。

刻意做成事後偵測而不是在寫入端多加判斷：寫入端再聰明也只是猜，而這裡有真正的證據 ——
**異動記錄裡，MAC 的變更發生在關聯之後**。也刻意不自動解除關聯：那同樣是猜。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.models.address import IPAddress
from app.models.device import Device
from app.models.ip_change_log import IPChangeLog
from app.models.section import Section
from app.models.subnet import Subnet
from app.services.anomaly import detect_stale_device_links


async def _seed(session, *, cidr: str, ip: str, mac_after_link: bool) -> IPAddress:
    sec = Section(name=f"sdl-{ip}")
    session.add(sec)
    await session.flush()
    sub = Subnet(section_id=sec.id, cidr=cidr)
    session.add(sub)
    await session.flush()
    dev = Device(name=f"dev-{ip}", type="server")
    session.add(dev)
    await session.flush()
    addr = IPAddress(subnet_id=sub.id, ip=ip, device_id=dev.id, mac="aa:bb:cc:dd:ee:01")
    session.add(addr)
    await session.flush()

    now = datetime.now(UTC)
    linked_at = now - timedelta(days=2)
    mac_at = now - timedelta(days=1) if mac_after_link else now - timedelta(days=3)
    # ip_text / subnet_id 是刪除後仍要保留歷史用的快照欄位，非空
    session.add(IPChangeLog(
        ip_id=addr.id, subnet_id=sub.id, ip_text=ip,
        event_type="edited", field="device_id",
        old_value=None, new_value=str(dev.id), source="user", created_at=linked_at,
    ))
    session.add(IPChangeLog(
        ip_id=addr.id, subnet_id=sub.id, ip_text=ip,
        event_type="edited", field="mac",
        old_value="aa:bb:cc:dd:ee:00", new_value="aa:bb:cc:dd:ee:01",
        source="system", created_at=mac_at,
    ))
    await session.commit()
    return addr


async def test_mac_changed_after_linking_is_flagged(db_session):
    addr = await _seed(db_session, cidr="10.90.0.0/24", ip="10.90.0.5", mac_after_link=True)
    hits = await detect_stale_device_links(db_session)
    assert any(h["ip_id"] == str(addr.id) for h in hits), (
        "MAC 在關聯之後才變 —— 這正是「位址換了主人」的證據，應該提出來"
    )
    hit = next(h for h in hits if h["ip_id"] == str(addr.id))
    assert hit["device"], "沒有講出掛在哪一台裝置，使用者無從判斷"
    assert hit["linked_at"] and hit["mac_changed_at"], (
        "沒有附上兩個時間點，看的人無法自己確認先後"
    )


async def test_mac_changed_before_linking_is_not_flagged(db_session):
    """關聯之前的 MAC 變更不算數 —— 那是掛上去的人當時就看得到的狀態。"""
    addr = await _seed(db_session, cidr="10.91.0.0/24", ip="10.91.0.5", mac_after_link=False)
    hits = await detect_stale_device_links(db_session)
    assert all(h["ip_id"] != str(addr.id) for h in hits), "把關聯之前的變更也報出來了"


async def test_detector_does_not_change_anything(db_session):
    """偵測就是偵測 —— 不可以順手解除關聯。要不要拆是人的決定。"""
    addr = await _seed(db_session, cidr="10.92.0.0/24", ip="10.92.0.5", mac_after_link=True)
    before = addr.device_id
    await detect_stale_device_links(db_session)
    await db_session.refresh(addr)
    assert addr.device_id == before, "偵測規則動到了資料"


async def _dual_nic(session, *, current: str) -> IPAddress:  # type: ignore[no-untyped-def]
    """雙網卡主機：兩個 IP 都關聯到同一台裝置；198.51.100.45 關聯之後 MAC 在兩張網卡之間來回換（ARP flux）。"""
    own, other = "bc:24:11:5e:01:01", "bc:24:11:5e:02:02"
    sec = Section(name="sdl-dual")
    session.add(sec)
    await session.flush()
    s1 = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    s2 = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    dev = Device(name="srv-dual", type="server")
    session.add_all([s1, s2, dev])
    await session.flush()
    a = IPAddress(subnet_id=s1.id, ip="198.51.100.45", device_id=dev.id, mac=current)
    b = IPAddress(subnet_id=s2.id, ip="203.0.113.45", device_id=dev.id, mac=other)
    session.add_all([a, b])
    await session.flush()
    now = datetime.now(UTC)
    session.add(IPChangeLog(ip_id=a.id, subnet_id=s1.id, ip_text=str(a.ip), event_type="edited", field="device_id",
                            old_value=None, new_value=str(dev.id), source="user", created_at=now - timedelta(days=30)))
    seq = [(own, other), (other, own)] * 3
    if current not in (own, other):
        seq.append((own, current))
    for i, (o, n) in enumerate(seq):
        session.add(IPChangeLog(ip_id=a.id, subnet_id=s1.id, ip_text=str(a.ip), event_type="mac_changed",
                                field="mac", old_value=o, new_value=n, source="scanner",
                                created_at=now - timedelta(days=10) + timedelta(hours=i)))
    await session.commit()
    return a


@pytest.mark.parametrize("cur", ["bc:24:11:5e:01:01", "bc:24:11:5e:02:02"])
async def test_flipping_between_the_devices_own_nics_is_not_stale(db_session, cur):
    """正式環境 2026-10-20：「裝置關聯可能過期」整頁是雙網卡主機 —— MAC 只是在自己兩張網卡之間換。"""
    a = await _dual_nic(db_session, current=cur)
    assert not [h for h in await detect_stale_device_links(db_session) if h["ip_id"] == str(a.id)]


async def test_a_mac_the_device_does_not_have_is_still_stale(db_session):
    a = await _dual_nic(db_session, current="00:00:5e:00:53:77")
    assert [h for h in await detect_stale_device_links(db_session) if h["ip_id"] == str(a.id)]
