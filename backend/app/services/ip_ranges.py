"""子網路內的位址範圍（集區）—— GitHub issue #40。

驗證、使用率，以及把「用途＝DHCP 集區」的手動範圍，整理成跟各整合同步回來的 DHCP 範圍
（`DHCPPoolRange`）同一個形狀，讓既有的使用端（清單的「在 DHCP 範圍內」、DHCP 集區使用率、
DHCP 範圍清單、AI 工具）直接算進去，不必各自再認識一種資料。
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ui_error import UiError
from app.models.address import IPAddress
from app.models.ip_range import IPRange
from app.models.subnet import Subnet

#: 找下一個可用位址時最多看幾個（/16 的範圍逐一掃會太久；前面這麼多都用掉了就不再找）
_FIRST_FREE_SCAN = 65536


def _addr(value: str, which: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        return ipaddress.ip_address(str(value).split("/")[0].strip())
    except ValueError as exc:
        raise UiError(f"「{value}」不是有效的 IP 位址", code="range_invalid_address",
                      value=str(value), which=which) from exc


async def validate_range(session: AsyncSession, subnet: Subnet, start: str, end: str, *,
                         exclude_id: uuid.UUID | None = None) -> tuple[str, str]:
    """檢查一段範圍能不能放進這個子網路；回傳正規化後的 (起, 迄)。

    - 起、迄都要是位址，而且同一個家族（IPv4／IPv6）
    - 起 ≤ 迄
    - 整段都在子網路的 CIDR 裡
    - 不跟同一個子網路裡的其他範圍重疊（緊鄰可以）—— 一個位址同時屬於「DHCP 集區」與
      「保留」沒有意義，而且會讓「這個 IP 在哪個範圍」答不出來
    """
    a, b = _addr(start, "start"), _addr(end, "end")
    if a.version != b.version:
        raise UiError("起迄位址的 IP 版本不同", code="range_family_mismatch")
    if int(a) > int(b):
        raise UiError(f"起始位址 {a} 比結束位址 {b} 大", code="range_start_after_end",
                      start=str(a), end=str(b))
    net = ipaddress.ip_network(str(subnet.cidr), strict=False)
    if a not in net or b not in net:
        raise UiError(f"範圍 {a}～{b} 超出子網路 {net}", code="range_outside_subnet",
                      start=str(a), end=str(b), cidr=str(net))
    others = (await session.execute(
        select(IPRange).where(IPRange.subnet_id == subnet.id))).scalars().all()
    for o in others:
        if exclude_id is not None and o.id == exclude_id:
            continue
        lo, hi = int(_addr(o.start_ip, "start")), int(_addr(o.end_ip, "end"))
        if int(a) <= hi and lo <= int(b):
            label = o.name or f"{_addr(o.start_ip, 'start')}～{_addr(o.end_ip, 'end')}"
            raise UiError(f"與既有範圍「{label}」重疊", code="range_overlap", other=label)
    return str(a), str(b)


async def ranges_with_usage(session: AsyncSession, subnet: Subnet) -> list[dict[str, Any]]:
    """子網路裡的每段範圍，連同大小、已有記錄的位址數、下一個可用位址。

    「已用」＝範圍內**已經存在的 IP 記錄**（與 DHCP 集區使用率同一個定義）：有人把固定 IP
    設在集區裡一樣吃掉可用量，而那正是應該被看見的事。
    """
    rows = (await session.execute(
        select(IPRange).where(IPRange.subnet_id == subnet.id).order_by(IPRange.start_ip)
    )).scalars().all()
    used_ints = sorted({int(ipaddress.ip_address(str(ip).split("/")[0]))
                        for (ip,) in (await session.execute(
                            select(IPAddress.ip).where(IPAddress.subnet_id == subnet.id))).all()})
    used_set = set(used_ints)
    out: list[dict[str, Any]] = []
    for r in rows:
        lo = int(ipaddress.ip_address(str(r.start_ip).split("/")[0]))
        hi = int(ipaddress.ip_address(str(r.end_ip).split("/")[0]))
        used = sum(1 for v in used_ints if lo <= v <= hi)
        first_free = None
        for v in range(lo, min(hi, lo + _FIRST_FREE_SCAN - 1) + 1):
            if v not in used_set:
                first_free = str(ipaddress.ip_address(v))
                break
        out.append({
            "id": r.id, "subnet_id": r.subnet_id,
            "start_ip": str(r.start_ip).split("/")[0], "end_ip": str(r.end_ip).split("/")[0],
            "purpose": r.purpose, "name": r.name, "description": r.description,
            "size": hi - lo + 1, "used": used, "first_free": first_free,
        })
    return out


@dataclass
class ManualDhcpPool:
    """手動定義的 DHCP 集區，長得跟 `DHCPPoolRange` 一樣（使用端讀的欄位名稱相同）。"""

    id: uuid.UUID
    source_id: uuid.UUID          # ＝子網路 id
    source_name: str
    subnet_cidr: str
    start_ip: str
    end_ip: str
    family: int
    source_type: str = "manual"
    source: str = "manual"
    synced_at: datetime | None = None


async def manual_dhcp_pools(session: AsyncSession,
                            subnet_ids: list[uuid.UUID] | None = None) -> list[ManualDhcpPool]:
    """用途＝DHCP 集區的手動範圍（`subnet_ids` 給了就只取那些子網路的）。"""
    stmt = (select(IPRange, Subnet.cidr).join(Subnet, Subnet.id == IPRange.subnet_id)
            .where(IPRange.purpose == "dhcp"))
    if subnet_ids is not None:
        stmt = stmt.where(IPRange.subnet_id.in_(subnet_ids))
    out: list[ManualDhcpPool] = []
    for r, cidr in (await session.execute(stmt.order_by(IPRange.start_ip))).all():
        start = str(r.start_ip).split("/")[0]
        out.append(ManualDhcpPool(
            id=r.id, source_id=r.subnet_id, source_name=r.name or "手動定義",
            subnet_cidr=str(cidr), start_ip=start, end_ip=str(r.end_ip).split("/")[0],
            family=ipaddress.ip_address(start).version))
    return out
