"""DHCP 範圍／集區的「已用」不算過期的舊記錄（2026-10-18 外部 AI 實測 MCP 回報）。

以前「已用」＝範圍內所有 IP 記錄：DHCP 客戶端離開後記錄還在（離線、沒有租約），同一段範圍
畫面與 AI 都說 50/51 用掉、沒有下一個可用位址，DHCP 伺服器自己只租出 31 個。客戶看到的是
「集區快滿了」的假告警，AI 回答的是「沒有空位」。

DHCP 範圍的「使用中」＝有租約、DHCP 固定分配、狀態保留／隔離，或沒有確定離線（上線或狀態未知
都保守算進來）。離線＋沒租約＋沒固定分配的記錄是「過期記錄」，另外列出數量，不算已用。
保留／其他用途的範圍不變：有記錄就是被占用。
"""
from __future__ import annotations

import uuid
from typing import Any

from app.models.address import IPAddress
from app.models.ip_range import IPRange
from app.models.section import Section
from app.models.subnet import Subnet
from app.services.dhcp_usage import pool_usage
from app.services.ip_ranges import ranges_with_usage

# (最後一碼, effective_status, in_dhcp_lease, dhcp_reserved, state)
_ROWS: list[tuple[int, str | None, bool, bool, str]] = [
    (100, "offline", False, False, "active"),       # 過期：離線、沒租約
    (101, "offline", False, False, "active"),       # 過期
    (102, "offline", True, False, "active"),        # 有租約（剛離線、租約還在）
    (103, "offline", False, True, "active"),        # DHCP 固定分配
    (104, "offline", False, False, "reserved"),     # 手動保留
    (105, "online (scanner)", False, False, "active"),  # 上線（固定 IP 設在集區裡也吃掉可用量）
    (106, None, False, False, "active"),            # 狀態未知：保守算
    (107, "unknown", False, False, "active"),
]


async def _site(db, purpose: str = "dhcp") -> tuple[Subnet, IPRange]:  # type: ignore[no-untyped-def]
    sec = Section(name=f"st-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    for last, eff, lease, resv, state in _ROWS:
        db.add(IPAddress(subnet_id=sub.id, ip=f"198.51.100.{last}", state=state, effective_status=eff,
                         in_dhcp_lease=lease, dhcp_reserved=resv))
    r = IPRange(subnet_id=sub.id, start_ip="198.51.100.100", end_ip="198.51.100.109", purpose=purpose)
    db.add(r)
    await db.commit()
    return sub, r


async def test_dhcp_range_does_not_count_stale_records(db_session) -> None:
    sub, _ = await _site(db_session)
    (row,) = await ranges_with_usage(db_session, sub)
    assert row["size"] == 10
    assert row["records"] == 8
    assert row["stale"] == 2
    assert row["used"] == 6
    assert row["free"] == 4
    # 下一個可用位址仍然要跳過已有記錄的位址（按下去是新增 IP，重複會被擋）
    assert row["first_free"] == "198.51.100.108"


async def test_reserved_range_still_counts_every_record(db_session) -> None:
    sub, _ = await _site(db_session, purpose="reserved")
    (row,) = await ranges_with_usage(db_session, sub)
    assert row["used"] == 8
    assert row["stale"] == 0
    assert row["free"] == 2


async def test_pool_usage_skips_stale_records(db_session) -> None:
    _, r = await _site(db_session)
    used, size = next((u, n) for p, u, n in await pool_usage(db_session) if p.id == r.id)
    assert (used, size) == (6, 10)


async def test_mcp_subnet_detail_reports_stale_and_free(db_session, admin_user) -> None:
    """AI 要看得到「範圍裡有幾筆過期記錄」，否則只能拿 used 跟 DHCP 伺服器比、得出錯的結論。"""
    from app.mcp.tools import TOOLS
    sub, _ = await _site(db_session)
    out: Any = await TOOLS["get_subnet_detail"]["fn"](db_session, user=admin_user, subnet_id=str(sub.id))
    (rng,) = out["ranges"]
    assert (rng["used"], rng["stale"], rng["records"], rng["free"]) == (6, 2, 8, 4)
