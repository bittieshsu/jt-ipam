"""沒有納管、但看得到在用的位址（unmanaged_sightings；使用者 2026-10-06）。

寫入：掃描代理在「自動收錄」關閉時掃到 IPAM 裡沒有的活位址（scan_agents.agent_report）。
讀取：子網路的指示計（「未納管」格子）與異常偵測的「未授權 IP」。LibreNMS 的 ARP 表本來就留著
未登錄的位址（arp_entries），讀的時候一起算，不重複存。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

#: 多久沒再看到就清掉
RETENTION = timedelta(days=30)
#: 指示計上顯示多久以內看到的（更久的仍保留給異常偵測與之後再看到時接續 first_seen）
GRID_WINDOW = timedelta(days=7)
_BATCH = 1000


async def record_sightings(
    session: AsyncSession, *, source: str, source_id: uuid.UUID | None, now: datetime,
    sightings: dict[tuple[uuid.UUID, str], dict[str, Any]],
) -> int:
    """整批 upsert（同一個子網路＋位址＋來源一列）。MAC／主機名稱這次沒有就保留上次的。"""
    from app.models.unmanaged_sighting import UnmanagedSighting

    rows = [{"id": uuid.uuid4(), "subnet_id": sid, "ip": ip, "source": source, "source_id": source_id,
             "mac": v.get("mac"), "hostname": v.get("hostname"), "first_seen_at": now, "last_seen_at": now}
            for (sid, ip), v in sightings.items()]
    for i in range(0, len(rows), _BATCH):
        stmt = pg_insert(UnmanagedSighting).values(rows[i:i + _BATCH])
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            constraint="uq_unmanaged_sighting",
            set_={"last_seen_at": ex.last_seen_at, "source_id": ex.source_id,
                  "mac": func.coalesce(ex.mac, UnmanagedSighting.mac),
                  "hostname": func.coalesce(ex.hostname, UnmanagedSighting.hostname)},
        )
        await session.execute(stmt)
    return len(rows)


async def purge(session: AsyncSession, *, now: datetime | None = None) -> int:
    """30 天沒再看到的拿掉（jt-ipam-sync 每輪一次）。"""
    from app.models.unmanaged_sighting import UnmanagedSighting
    cutoff = (now or datetime.now(UTC)) - RETENTION
    res = await session.execute(delete(UnmanagedSighting).where(UnmanagedSighting.last_seen_at < cutoff))
    return int(res.rowcount or 0)


async def for_subnet(session: AsyncSession, subnet_id: uuid.UUID, *, now: datetime | None = None,
                     window: timedelta = GRID_WINDOW) -> list[dict[str, Any]]:
    """這個子網路裡、IPAM 沒有記錄、最近看得到的位址（掃描代理目擊＋LibreNMS ARP），一個位址一筆。"""
    from app.models.address import IPAddress
    from app.models.librenms import ARPEntry
    from app.models.unmanaged_sighting import UnmanagedSighting

    since = (now or datetime.now(UTC)) - window
    out: dict[str, dict[str, Any]] = {}

    def _merge(ip: str, source: str, last: datetime | None, mac: str | None, hostname: str | None) -> None:
        e = out.setdefault(ip, {"ip": ip, "last_seen_at": None, "sources": set(), "mac": None, "hostname": None})
        e["sources"].add(source)
        if last and (e["last_seen_at"] is None or last > e["last_seen_at"]):
            e["last_seen_at"] = last
            e["mac"] = mac or e["mac"]
            e["hostname"] = hostname or e["hostname"]
        e["mac"] = e["mac"] or mac
        e["hostname"] = e["hostname"] or hostname

    s_host = func.host(UnmanagedSighting.ip)
    reg_s = select(IPAddress.id).where(IPAddress.subnet_id == subnet_id, func.host(IPAddress.ip) == s_host).exists()
    for ip, source, last, mac, hostname in (await session.execute(
            select(s_host, UnmanagedSighting.source, UnmanagedSighting.last_seen_at,
                   UnmanagedSighting.mac, UnmanagedSighting.hostname)
            .where(UnmanagedSighting.subnet_id == subnet_id, UnmanagedSighting.last_seen_at >= since, ~reg_s))).all():
        _merge(str(ip), str(source), last, mac, hostname)

    a_host = func.host(ARPEntry.ip)
    reg_a = select(IPAddress.id).where(IPAddress.subnet_id == subnet_id, func.host(IPAddress.ip) == a_host).exists()
    for ip, source, last, mac in (await session.execute(
            select(a_host, ARPEntry.source, func.max(ARPEntry.last_seen_at), func.max(func.text(ARPEntry.mac)))
            .where(ARPEntry.subnet_id == subnet_id, ARPEntry.last_seen_at >= since, ~reg_a)
            .group_by(a_host, ARPEntry.source))).all():
        _merge(str(ip), f"arp:{source}", last, str(mac) if mac else None, None)

    res = []
    for e in out.values():
        e["sources"] = sorted(e["sources"])
        e["last_seen_at"] = e["last_seen_at"].isoformat() if e["last_seen_at"] else None
        res.append(e)
    return res
