"""DHCP 集區的使用率。

「已用」＝**範圍內已經存在的 IP 記錄**，而不是只算有租約的。使用者關心的是
「還有多少位址可以發出去」：有人把固定 IP 設在集區的範圍裡（常見的設定失誤）
一樣吃掉可用量，而且那正是應該被看見的事。
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPPoolRange


def _range_size(start: str, end: str) -> int:
    """範圍內的位址數。算不出來（起 > 迄、不是位址、家族不同）回 0。

    回 0 而不是丟例外：這些值是從外部設備同步回來的，壞掉的一筆不該讓整輪告警中斷。
    """
    try:
        a = ipaddress.ip_address(str(start))
        b = ipaddress.ip_address(str(end))
    except ValueError:
        return 0
    if a.version != b.version or int(b) < int(a):
        return 0
    return int(b) - int(a) + 1


async def _scope_of(session: AsyncSession, source_type: str, source_id: Any) -> set[str] | None:
    """整合設定的範圍子網路（沒設、或找不到這個整合回 None）。"""
    import importlib

    from app.services.change_impact.sources import _SPECS
    spec = next((sp for sp in _SPECS if sp[2] == source_type), None)
    if spec is None:
        return None
    model = getattr(importlib.import_module(spec[0]), spec[1], None)
    row = await session.get(model, source_id) if model is not None else None
    scope = getattr(row, "scope_subnet_ids", None) if row is not None else None
    return {str(x) for x in scope} if scope else None


async def pool_subnet(session: AsyncSession, pool: Any) -> Any:
    """集區屬於哪個子網路；判斷不了（重疊網段裡有好幾個一樣的）回 None，不猜。

    手動集區本來就掛在子網路上。整合同步回來的集區只有 CIDR 字串：先找包含整段範圍的子網路，
    有 CIDR 就只留同一段，再用整合設定的範圍縮小，最後取最具體（首碼最長）的那一層。
    """
    from app.services.ip_ranges import ManualDhcpPool
    if isinstance(pool, ManualDhcpPool):
        return pool.source_id
    rows = (await session.execute(text("""
        SELECT id, cidr::text AS cidr, masklen(cidr) AS plen FROM subnets
         WHERE archived_at IS NULL AND cidr >>= CAST(:lo AS inet) AND cidr >>= CAST(:hi AS inet)
    """), {"lo": str(pool.start_ip), "hi": str(pool.end_ip)})).all()
    if pool.subnet_cidr:
        try:
            want = str(ipaddress.ip_network(str(pool.subnet_cidr), strict=False))
            same = [r for r in rows if str(ipaddress.ip_network(r.cidr, strict=False)) == want]
            rows = same or rows
        except ValueError:
            pass
    if len(rows) > 1:
        scope = await _scope_of(session, pool.source_type, pool.source_id)
        if scope:
            rows = [r for r in rows if str(r.id) in scope] or rows
    if not rows:
        return None
    best = max(r.plen for r in rows)
    top = [r for r in rows if r.plen == best]
    return top[0].id if len(top) == 1 else None


@dataclass
class PoolGroup:
    """一個設定的 DHCP 範圍（扣掉排除之後可能被切成好幾段）。欄位名稱跟 DHCPPoolRange 一樣，
    使用端（pool_subnet、告警）照原本的讀法。start_ip／end_ip 是設定的整段範圍。"""
    source_type: str
    source_id: Any
    source_name: str | None
    subnet_cidr: str | None
    start_ip: str
    end_ip: str
    segments: list[tuple[str, str]] = field(default_factory=list)
    id: Any = None

    @property
    def excluded(self) -> int:
        """設定的範圍裡被排除、不發放的位址數。"""
        whole = _range_size(self.start_ip, self.end_ip)
        return max(0, whole - sum(_range_size(a, b) for a, b in self.segments)) if whole else 0


def group_pools(rows: list[Any]) -> list[PoolGroup]:
    """把扣掉排除後切出來的段，依「來源＋子網路＋設定的範圍」合回一個集區（0210 以前的列沒有
    pool_start，每一段就是自己的範圍，跟以前一樣）。"""
    groups: dict[tuple[str, str, str, str, str], PoolGroup] = {}
    for r in rows:
        start = getattr(r, "pool_start", None) or r.start_ip
        end = getattr(r, "pool_end", None) or r.end_ip
        key = (r.source_type, str(r.source_id), r.subnet_cidr or "", start, end)
        g = groups.get(key)
        if g is None:
            g = groups[key] = PoolGroup(source_type=r.source_type, source_id=r.source_id,
                                        source_name=r.source_name, subnet_cidr=r.subnet_cidr,
                                        start_ip=start, end_ip=end, id=r.id)
        g.segments.append((r.start_ip, r.end_ip))
    return list(groups.values())


async def pool_usage(session: AsyncSession) -> list[tuple[Any, int, int]]:
    """回傳每個集區的 (集區, 已用, 總數)。已用只算集區所屬子網路的 IP 記錄（重疊網段不混算）。

    集區以 DHCP 伺服器上設定的範圍為單位：排除區間切出來的各段合併計算，總數已扣掉排除的位址、
    排除位址上的 IP 記錄（多半就是設了固定 IP 的設備）不算已用（客戶 2026-10-12）。
    """
    from app.services.ip_ranges import manual_dhcp_pools
    # 手動定義的 DHCP 集區（子網路內的位址範圍，issue #40）欄位與 DHCPPoolRange 同名，一起算
    pools: list[Any] = [*group_pools(list((await session.execute(select(DHCPPoolRange))).scalars().all())),
                        *await manual_dhcp_pools(session)]
    out: list[tuple[Any, int, int]] = []
    for pool in pools:
        segments = getattr(pool, "segments", None) or [(pool.start_ip, pool.end_ip)]
        size = sum(_range_size(a, b) for a, b in segments)
        if size == 0:
            out.append((pool, 0, 0))
            continue
        sid = await pool_subnet(session, pool)
        if sid is None:
            # 不知道是哪個子網路：不拿別人的 IP 來算（寧可不發，也不發假的「快滿了」）
            out.append((pool, 0, size))
            continue
        used = 0
        for a, b in segments:
            used += (await session.execute(text("""
                SELECT count(*) FROM ip_addresses
                 WHERE subnet_id = :sid AND ip BETWEEN CAST(:lo AS inet) AND CAST(:hi AS inet)
            """), {"sid": sid, "lo": str(a), "hi": str(b)})).scalar_one()
        out.append((pool, int(used), size))
    return out
