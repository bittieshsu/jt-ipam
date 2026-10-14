"""「MAC 只是寫法不同」不是 MAC 變動（使用者 2026-10-14：清掉、而且不要再產生）。

正式站台 2026-09-17～10-05 累積 2,168 筆假的 MAC 變動：同一個 MAC，一邊有冒號一邊沒有
（`bc:45:5b:cf:ba:d0` → `bc455bcfbad0`）。它們出現在 IP 時間軸、MAC 歷程，還會讓「裝置關聯可能過期」
誤報（那條規則看的就是「關聯之後 MAC 換過」）。
- 寫入端：同一個 MAC 只是寫法不同 → 不記
- migration 0212：清掉既有的這種記錄
- 異常偵測：就算還有這種記錄也不算 MAC 換過
"""
from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.models.address import IPAddress
from app.models.device import Device
from app.models.ip_change_log import IPChangeLog
from app.models.section import Section
from app.models.subnet import Subnet
from sqlalchemy import func, select, text

pytestmark = pytest.mark.anyio


async def _ip(db, mac: str = "02:00:00:00:00:01") -> IPAddress:  # type: ignore[no-untyped-def]
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    ip = IPAddress(subnet_id=sub.id, ip=f"198.51.100.{uuid.uuid4().int % 200 + 1}", mac=mac, state="used")
    db.add(ip)
    await db.flush()
    return ip


async def _count(db, ip) -> int:  # type: ignore[no-untyped-def]
    return (await db.execute(select(func.count()).select_from(IPChangeLog).where(
        IPChangeLog.ip_id == ip.id, IPChangeLog.field == "mac"))).scalar_one()


async def test_format_only_mac_change_is_not_logged(db_session) -> None:
    from app.services.ip_history import log_change
    ip = await _ip(db_session)
    await log_change(db_session, ip=ip, event_type="arp_changed", field="mac",
                     old="02:00:00:00:00:01", new="020000000001", source="librenms")
    await log_change(db_session, ip=ip, event_type="arp_changed", field="mac",
                     old="02-00-00-00-00-01", new="02:00:00:00:00:01".upper(), source="librenms")
    await db_session.flush()
    assert await _count(db_session, ip) == 0
    await log_change(db_session, ip=ip, event_type="arp_changed", field="mac",
                     old="02:00:00:00:00:01", new="02:00:00:00:00:02", source="librenms")
    await db_session.flush()
    assert await _count(db_session, ip) == 1, "真的換了 MAC 照記"


def _migration():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0212_purge_fmt_mac_changes.py"
    spec = importlib.util.spec_from_file_location("m0212", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


async def test_migration_purges_only_format_only_rows(db_session) -> None:
    ip = await _ip(db_session)
    db_session.add_all([
        IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), event_type="arp_changed", field="mac",
                    old_value="02:00:00:00:00:01", new_value="020000000001", source="librenms"),
        IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), event_type="arp_changed", field="mac",
                    old_value="02:00:00:00:00:01", new_value="02:00:00:00:00:02", source="librenms"),
        IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), event_type="arp_changed", field="mac",
                    old_value=None, new_value="02:00:00:00:00:03", source="librenms"),
        IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), event_type="updated", field="hostname",
                    old_value="a", new_value="A", source="manual"),
    ])
    await db_session.flush()
    await db_session.execute(text(_migration().PURGE_SQL))
    left = sorted((r.old_value or "-", r.new_value) for r in (await db_session.execute(
        select(IPChangeLog).where(IPChangeLog.ip_id == ip.id))).scalars())
    assert left == [("-", "02:00:00:00:00:03"), ("02:00:00:00:00:01", "02:00:00:00:00:02"), ("a", "A")]


async def test_stale_link_ignores_format_only_mac_rows(db_session) -> None:
    """關聯之後只有「寫法不同」的 MAC 記錄 → 不是關聯過期（以前會誤報）。"""
    from app.services.anomaly import detect_stale_device_links
    ip = await _ip(db_session)
    dev = Device(name=f"dev-{uuid.uuid4().hex[:6]}", type="server")
    db_session.add(dev)
    await db_session.flush()
    ip.device_id = dev.id
    now = datetime.now(UTC)
    db_session.add_all([
        IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), event_type="updated", field="device_id",
                    old_value=None, new_value=str(dev.id), source="manual", created_at=now - timedelta(days=10)),
        IPChangeLog(ip_id=ip.id, subnet_id=ip.subnet_id, ip_text=str(ip.ip), event_type="arp_changed", field="mac",
                    old_value="02:00:00:00:00:01", new_value="020000000001", source="librenms",
                    created_at=now - timedelta(days=1)),
    ])
    await db_session.flush()
    rows = [r for r in await detect_stale_device_links(db_session) if r["ip_id"] == str(ip.id)]
    assert rows == []
