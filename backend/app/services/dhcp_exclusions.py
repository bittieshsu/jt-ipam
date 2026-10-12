"""DHCP 排除區間：區間相減＋共用寫入層（0207，客戶 2026-10-12）。

排除區間＝在發放範圍裡、但 DHCP 伺服器不會發給任何人的位址（Windows DHCP 的 Exclusion、
FortiGate 的 exclude-range、Technitium 的 exclusions、Gaia 的 exclude ip-pool）。常見用途是
在範圍中間挖掉幾個位址給設固定 IP 的設備。

兩件事分開做：
- 發放範圍寫入 `dhcp_pool_ranges` 之前先用 `subtract()` 扣掉排除區間 —— 讀範圍的地方
  （清單「在 DHCP 範圍內」、集區使用率、ARP 自動建立、指示計底線、IP 詳細資料）因此不必各自
  認識排除區間，也不會有「有些地方扣了、有些沒扣」
- 排除區間本身寫進 `dhcp_exclusions`（`replace_exclusions()`），只用來在 IP 上正面標示「DHCP 排除」
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class Exclusion:
    start: str
    end: str
    subnet_cidr: str | None = None


def _addr(v: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(str(v or "").split("/")[0].strip())
    except ValueError:
        return None


def _pair(start: Any, end: Any) -> tuple[int, int, int] | None:
    """(家族, 小, 大)；位址不合法或家族不同回 None。"""
    a, b = _addr(start), _addr(end)
    if a is None or b is None or a.version != b.version:
        return None
    lo, hi = sorted((int(a), int(b)))
    return a.version, lo, hi


def subtract(start: Any, end: Any, cuts: Iterable[tuple[Any, Any]]) -> list[tuple[str, str]]:
    """[start, end] 扣掉 cuts 之後剩下的幾段（依位址排序）。

    壞掉的排除項（不是位址、家族不同）略過 —— 不讓一筆壞資料把整個發放範圍弄不見；
    範圍本身不合法就回空清單。
    """
    base = _pair(start, end)
    if base is None:
        return []
    fam, lo, hi = base
    holes = sorted(p[1:] for p in (_pair(s, e) for s, e in cuts) if p is not None and p[0] == fam)
    out: list[tuple[int, int]] = []
    cur = lo
    for s, e in holes:
        if e < cur or s > hi:
            continue
        if s > cur:
            out.append((cur, s - 1))
        cur = max(cur, e + 1)
        if cur > hi:
            break
    if cur <= hi:
        out.append((cur, hi))
    return [(str(ipaddress.ip_address(a)), str(ipaddress.ip_address(b))) for a, b in out]


def clip(start: Any, end: Any, cuts: Iterable[tuple[Any, Any]]) -> list[tuple[str, str]]:
    """排除區間裡落在 [start, end] 內的部分（範圍外的排除項對這個範圍沒有意義）。"""
    base = _pair(start, end)
    if base is None:
        return []
    fam, lo, hi = base
    out: list[tuple[str, str]] = []
    for s, e in cuts:
        p = _pair(s, e)
        if p is None or p[0] != fam or p[2] < lo or p[1] > hi:
            continue
        out.append((str(ipaddress.ip_address(max(lo, p[1]))), str(ipaddress.ip_address(min(hi, p[2])))))
    return out


async def replace_exclusions(
    session: AsyncSession,
    *,
    source_type: str,
    source_id: uuid.UUID,
    source_name: str | None,
    engine: str,
    rows: list[Exclusion],
) -> int:
    """以這個來源的最新結果取代它先前寫入的排除區間（不動其他來源的列）。"""
    from app.models.dhcp import DHCPExclusion

    await session.execute(delete(DHCPExclusion).where(
        DHCPExclusion.source_type == source_type, DHCPExclusion.source_id == source_id))
    now = datetime.now(UTC)
    seen: set[tuple[str, str, str | None]] = set()
    for r in rows:
        p = _pair(r.start, r.end)
        if p is None:
            continue
        fam, lo, hi = p
        a, b = str(ipaddress.ip_address(lo)), str(ipaddress.ip_address(hi))
        key = (a, b, r.subnet_cidr)
        if key in seen:
            continue
        seen.add(key)
        session.add(DHCPExclusion(
            source_type=source_type, source_id=source_id, source_name=source_name,
            subnet_cidr=(r.subnet_cidr or None), start_ip=a, end_ip=b,
            family=fam, source=engine, synced_at=now))
    return len(seen)
