"""自動建立 IP 記錄時「該放進哪個子網路」的共用判斷。

整合看到一個 IPAM 裡還沒有的 IP（DHCP 租約、LibreNMS 裝置…）時，可以自動建一筆。
難的不是建，是**建到哪個子網路** —— 本專案的核心情境就是重疊網段（多個單位各自
擁有 192.168.1.0/24），建錯單位比不建更糟：資料會靜靜地掛到別人名下。

規則（原本寫在 librenms.py，現在三個整合共用一份）：
- 多層巢狀（10.0.0.0/8 與 10.1.1.0/24 都包含）→ 取**最長首碼**那個，最精確者贏。
- **同長度多個都包含**（真正的重疊網段）→ **不建**。無從得知是誰的，猜就是猜錯。
- 沒有任何既有子網路包含 → **不建**（不會憑空生出子網路）。
- `scope_ids` 有值時只在那些子網路內找 —— 整合設定頁的「關聯子網路」正是用來消除
  重疊歧義的：範圍縮到自己那組，同長度多重命中自然就不會發生。
"""

from __future__ import annotations

import ipaddress
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subnet import Subnet

# (network, subnet_id)，依首碼長度由長到短
SubnetCandidates = list[tuple[Any, Any]]


async def addable_subnets(
    session: AsyncSession, scope_ids: set[Any] | list[Any] | None,
) -> SubnetCandidates:
    """可自動建立 IP 的候選子網路，依首碼長度由長到短（最精確優先）。

    `scope_ids` 有值＝只在這些子網路內建（重疊網段下的安全做法）；空＝全部既有子網路。
    """
    stmt = select(Subnet.id, Subnet.cidr)
    if scope_ids:
        stmt = stmt.where(Subnet.id.in_(list(scope_ids)))
    rows = (await session.execute(stmt)).all()
    nets: SubnetCandidates = []
    for sid, cidr in rows:
        try:
            nets.append((ipaddress.ip_network(str(cidr), strict=False), sid))
        except ValueError:
            continue
    nets.sort(key=lambda x: x[0].prefixlen, reverse=True)
    return nets


def pick_subnet_for_ip(nets: SubnetCandidates, aip: Any) -> Any | None:
    """挑「唯一且最精確」包含此 IP 的子網路；歧義或沒有命中都回 None（不建）。"""
    containing = [(net, sid) for net, sid in nets if aip in net]
    if not containing:
        return None
    maxlen = max(net.prefixlen for net, _ in containing)
    best = [sid for net, sid in containing if net.prefixlen == maxlen]
    return best[0] if len(best) == 1 else None


def subnet_for_ip_str(nets: SubnetCandidates, ip: str) -> UUID | None:
    """字串版：不合法的 IP 直接回 None，呼叫端不必自己先驗一次。"""
    try:
        aip = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return pick_subnet_for_ip(nets, aip)
