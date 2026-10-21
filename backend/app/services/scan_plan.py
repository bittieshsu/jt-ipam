"""掃描計畫：交給掃描代理「這個子網路每一輪要掃哪些位址」（客戶 2026-10-11，migration 0205）。

起因：前人把主站台設成 10.0.0.0/8，掃描直接炸掉。代理以前只拿到 CIDR、每輪照順序掃下一段 4,096 個，
/8 要 4,096 輪（約 14 天）才繞一圈，上線門檻 30 分鐘 → 設備幾乎永遠被判成離線；系統的建議也只有
「把子網路切小」，IPAM 只好把網段記錯（設備實際的遮罩是 /8）。掃描成本應該由「有多少設備」決定，
而不是「子網路有多大」。

兩層：
- 已知位址（已登記的 IP、ARP_RECENT_DAYS 天內 ARP 看過的）每一輪都掃 → 上線狀態不管子網路多大都準確
- 其餘位址每輪抽 DISCOVER_PER_CYCLE 個慢慢找新設備（探索）；不算進上線偵測的負載（scan_load）

- 鄰近探索（客戶 2026-10-20）：已經有設備的 /24 裡其餘的位址，每輪優先掃 NEAR_PER_CYCLE 個、很快繞完一圈。
  /8 的全域探索要約 57 天繞一圈，新接的印表機（10.0.0.54，旁邊的 .52、.55 都在）五、六個小時都沒被發現；
  新設備最可能出現在已經有設備的網段。舊代理不認得 near，照舊只有全域探索（不會壞）。

計畫的形狀（`None`＝照舊全掃；舊代理不認得 plan 欄位也是照舊全掃，不會壞）：
    {"mode": "targets", "known": [[起, 迄], …],
     "discover": {"per_cycle": N, "near": [[起, 迄], …], "near_per_cycle": M} | None}
`known` 是連續位址壓成的區段（不展開，位址範圍可能是一整段 /16）。代理目前只掃 IPv4。
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.models.address import IPAddress
from app.models.ip_range import IPRange
from app.models.librenms import ARPEntry

#: 自動模式下照舊全掃的上限（＝代理單輪上限的預設值 JT_IPAM_MAX_HOSTS）
AUTO_SWEEP_MAX = 4096
#: 探索：每輪抽幾個未知位址（/8 約 57 天繞一圈；/16 約 5 小時）
DISCOVER_PER_CYCLE = 1024
#: 鄰近探索：每輪掃幾個（已經有設備的 /24 裡其餘的位址；30 個 /24 約 8 輪＝40 分鐘繞完）
NEAR_PER_CYCLE = 1024
#: 鄰近探索最多涵蓋幾個 /24（設備最多的優先；6 萬多個位址）
NEAR_BLOCK_CAP = 256
#: ARP 看過多久以內算已知
ARP_RECENT_DAYS = 7
#: 已知位址最多送幾個（超過就截斷：一個子網路幾十萬台設備，應該拆給多台代理）
KNOWN_CAP = 65536

SCOPES = ("auto", "all", "known", "ranges")


def usable_count(cidr: str) -> int:
    net = ipaddress.ip_network(str(cidr), strict=False)
    return max(net.num_addresses - (2 if net.prefixlen < net.max_prefixlen - 1 else 0), 0)


def _ip(n: int) -> str:
    return str(ipaddress.IPv4Address(n))


def to_runs(ints: Iterable[int]) -> list[list[str]]:
    """排序、去重後把連續位址壓成 [起, 迄] 區段。"""
    out: list[list[int]] = []
    for n in sorted(set(ints)):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return [[_ip(a), _ip(b)] for a, b in out]


def merge_ranges(ranges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """合併重疊／相鄰的位址範圍（只看端點，不展開）。"""
    spans = sorted((int(ipaddress.ip_address(str(a))), int(ipaddress.ip_address(str(b)))) for a, b in ranges)
    out: list[list[int]] = []
    for a, b in spans:
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[_ip(a), _ip(b)] for a, b in out]


def near_addresses(cidr: str, known: set[int]) -> list[int]:
    """已經有設備的 /24 裡其餘的主機位址（設備最多的 NEAR_BLOCK_CAP 個 /24；不含網路／廣播位址與已知位址）。"""
    net = ipaddress.ip_network(str(cidr), strict=False)
    lo, hi = int(net.network_address) + 1, int(net.broadcast_address) - 1
    counts: dict[int, int] = {}
    for n in known:
        counts[n >> 8] = counts.get(n >> 8, 0) + 1
    blocks = sorted(counts, key=lambda b: (-counts[b], b))[:NEAR_BLOCK_CAP]
    out: list[int] = []
    for b in sorted(blocks):
        for n in range(max(b << 8, lo), min((b << 8) + 255, hi) + 1):
            if n not in known:
                out.append(n)
    return out


def needs_plan(cidr: str, scope: str) -> bool:
    net = ipaddress.ip_network(str(cidr), strict=False)
    if not isinstance(net, ipaddress.IPv4Network) or scope == "all":
        return False
    if scope == "auto":
        return usable_count(cidr) > AUTO_SWEEP_MAX
    return scope in ("known", "ranges")


async def plans(session: AsyncSession, subnets: Iterable[Any]) -> dict[uuid.UUID, dict[str, Any] | None]:
    """每個子網路的掃描計畫（None＝照舊全掃）。subnets 要有 id、cidr、scan_scope。整批查，不逐筆。"""
    subs = list(subnets)
    out: dict[uuid.UUID, dict[str, Any] | None] = {s.id: None for s in subs}
    planned = [s for s in subs if needs_plan(str(s.cidr), s.scan_scope or "auto")]
    if not planned:
        return out
    known_ids = [s.id for s in planned if (s.scan_scope or "auto") in ("auto", "known")]
    range_ids = [s.id for s in planned if s.scan_scope == "ranges"]

    known: dict[uuid.UUID, set[int]] = {sid: set() for sid in known_ids}
    if known_ids:
        rows = (await session.execute(select(IPAddress.subnet_id, IPAddress.ip).where(
            in_values(IPAddress.subnet_id, known_ids), func.family(IPAddress.ip) == 4))).all()
        since = datetime.now(UTC) - timedelta(days=ARP_RECENT_DAYS)
        rows += (await session.execute(select(ARPEntry.subnet_id, ARPEntry.ip).where(
            in_values(ARPEntry.subnet_id, known_ids), ARPEntry.last_seen_at >= since,
            func.family(ARPEntry.ip) == 4))).all()
        for sid, ip in rows:
            known[sid].add(int(ipaddress.ip_address(str(ip).split("/")[0])))

    ranges: dict[uuid.UUID, list[tuple[str, str]]] = {sid: [] for sid in range_ids}
    if range_ids:
        for sid, a, b in (await session.execute(select(IPRange.subnet_id, IPRange.start_ip, IPRange.end_ip).where(
                in_values(IPRange.subnet_id, range_ids), func.family(IPRange.start_ip) == 4))).all():
            ranges[sid].append((str(a).split("/")[0], str(b).split("/")[0]))

    for s in planned:
        scope = s.scan_scope or "auto"
        if scope == "ranges":
            out[s.id] = {"mode": "targets", "known": merge_ranges(ranges[s.id]), "discover": None}
            continue
        ints = sorted(known[s.id])[:KNOWN_CAP]
        plan: dict[str, Any] = {"mode": "targets", "known": to_runs(ints),
                                "discover": ({"per_cycle": DISCOVER_PER_CYCLE,
                                              "near": to_runs(near_addresses(str(s.cidr), known[s.id])),
                                              "near_per_cycle": NEAR_PER_CYCLE} if scope == "auto" else None)}
        if len(known[s.id]) > KNOWN_CAP:
            plan["known_truncated"] = len(known[s.id])
        out[s.id] = plan
    return out
