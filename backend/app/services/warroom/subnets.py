"""總覽與容量共用：可見子網路的容量、已用、保留、是否逾時（一次聚合查詢，不逐列載入）。

- 已用＝這個子網路的 IP 記錄數（與子網路頁、儀表板的配置率同一個定義）；保留＝其中 state=reserved
- 逾時＝負責的掃描代理超過 STALE_AFTER 沒有回報（與健康告警同一個門檻）；沒有指定掃描代理的子網路是
  「未掃描」，不是正常也不是逾時
- IPv6：容量是天文數字，不算配置率
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.models.address import IPAddress
from app.models.scan_agent import ScanAgent
from app.models.subnet import Subnet
from app.services.subnet import host_count

STALE_AFTER = timedelta(minutes=30)


@dataclass
class SubnetFacts:
    id: Any
    cidr: str
    label: str
    version: int
    capacity: int
    used: int = 0
    reserved: int = 0
    location_id: Any = None
    section_id: Any = None
    customer_id: Any = None
    scan: str = "unscanned"          # ok／stale／unscanned
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def pct(self) -> float | None:
        if self.version != 4 or not self.capacity:
            return None
        return round(self.used * 100 / self.capacity, 1)


async def load(session: AsyncSession, visible: set[Any] | None, *, now: datetime | None = None) -> list[SubnetFacts]:
    now = now or datetime.now(UTC)
    stmt = select(Subnet).where(Subnet.archived_at.is_(None))
    if visible is not None:
        if not visible:
            return []
        stmt = stmt.where(in_values(Subnet.id, list(visible)))
    subnets = list((await session.execute(stmt)).scalars().all())
    if not subnets:
        return []
    ids = [s.id for s in subnets]
    counts = {r[0]: (int(r[1]), int(r[2] or 0)) for r in (await session.execute(
        select(IPAddress.subnet_id, func.count(),
               func.count().filter(IPAddress.state == "reserved"))
        .where(in_values(IPAddress.subnet_id, ids)).group_by(IPAddress.subnet_id))).all()}
    agent_ids = {s.scan_agent_id for s in subnets if s.scan_agent_id}
    seen = dict((await session.execute(select(ScanAgent.id, ScanAgent.last_seen_at).where(
        in_values(ScanAgent.id, list(agent_ids))))).all()) if agent_ids else {}

    out: list[SubnetFacts] = []
    for s in subnets:
        try:
            net = ipaddress.ip_network(str(s.cidr), strict=False)
        except ValueError:
            continue
        used, reserved = counts.get(s.id, (0, 0))
        if s.scan_agent_id:
            last = seen.get(s.scan_agent_id)
            scan = "ok" if last and now - last <= STALE_AFTER else "stale"
        else:
            scan = "unscanned"
        out.append(SubnetFacts(
            id=s.id, cidr=str(net), label=(s.description or "").strip() or str(net), version=net.version,
            capacity=host_count(net), used=used, reserved=reserved, location_id=s.location_id,
            section_id=s.section_id, customer_id=s.customer_id, scan=scan))
    return out
