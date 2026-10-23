"""戰情室 08 IP 統計（2026-10-23 新增主題；規格：使用者交付的 JT-IPAM-Warroom-Expansion SPEC.md §5）。

分工：總覽看待處理事項、容量看逐子網路使用率；這一頁只看「納管規模與分布」。

資料口徑（畫面上的說明與這裡一致，definitions 一起回給畫面）：
- 範圍：觀看者看得到 ∩ 戰情室顯示範圍的子網路（common.scoped_visibility），不含已封存的子網路；
  與總覽、容量的「已登記 IP」同一個數字（subnets.load）
- 已登記 IP＝這些子網路裡的 IP 記錄數。一筆記錄只屬於一個子網路（subnet_id），所以子網路排行加總
  一定等於總數；父子子網路各算各的直屬記錄，不會重複累加
- 狀態分布：effective_status 逐值明確對應（status_class）：
    online／online (<來源>)／online (arp) → 上線（與儀表板同一個定義；只有 ARP 撐著的另外帶 arp_only 數）
    offline* → 離線
    空值／unknown／其他任何值 → 未知
  三者相加＝已登記 IP（每一筆都歸到一類）
- 方格：三列共用同一個尺度「每格 N 個 IP」（N 取 1、2、5、10、20、50…，讓最多的那一列放得下
  ROW_CELLS 格）；不足一格的尾數算一格（有值的狀態至少一格），精確數字看旁邊的計數
- IPv4／IPv6：依子網路的位址族分開計數（不混算）；不提供全域 IPv4 容量比例（父子重疊、/31 /32、
  不同租戶同 CIDR 的容量口徑還沒定，規格 §5.3）
- 稽核事件：最近 24 小時的稽核記錄數，是操作紀錄、不是告警。稽核記錄只有管理員（戰情室裡另含
  「戰情室檢視者」）看得到，其他人回 None（畫面顯示「無權限」，不顯示 0）。設定了顯示範圍時只算
  範圍內子網路與其中 IP 的記錄
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.models.address import IPAddress
from app.services.warroom import subnets as sn
from app.services.warroom.common import (
    full_access,
    paginate,
    scope_active,
    scoped_visibility,
    section,
)

PER_PAGE = 4
#: 每一列狀態方格最多幾格（40 欄 × 4 排）
ROW_COLS = 40
ROW_CELLS = 160
STATUSES = ("online", "offline", "unknown")
AUDIT_WINDOW = timedelta(hours=24)
#: 稽核記錄裡代表 IP 記錄的 object_type（兩種寫法都有）
IP_OBJECT_TYPES = ("ip", "ip_address")


def status_class(value: str | None) -> str:
    """effective_status → online／offline／unknown（每一個值都有歸屬）。"""
    v = (value or "").strip().lower()
    if v.startswith("online"):
        return "online"
    if v.startswith("offline"):
        return "offline"
    return "unknown"


def cell_unit(largest: int, max_cells: int = ROW_CELLS) -> int:
    """一格代表幾個 IP：1、2、5、10、20、50、100…，讓最多的那一列不超過 max_cells 格。"""
    unit, step = 1, 0
    while math.ceil(largest / unit) > max_cells:
        step += 1
        unit = (1, 2, 5)[step % 3] * 10 ** (step // 3)
    return unit


def cells_for(count: int, unit: int) -> int:
    """這個數量畫幾格：尾數不足一格也算一格（0 才是 0 格）。"""
    return math.ceil(count / unit) if count > 0 else 0


async def _status_counts(session: AsyncSession, ids: list[Any]) -> tuple[dict[str, int], int]:
    """(各狀態數, 其中只有 ARP 撐著的上線數)"""
    counts = dict.fromkeys(STATUSES, 0)
    arp_only = 0
    if not ids:
        return counts, 0
    for value, n in (await session.execute(
            select(IPAddress.effective_status, func.count())
            .where(in_values(IPAddress.subnet_id, ids)).group_by(IPAddress.effective_status))).all():
        counts[status_class(value)] += int(n)
        if (value or "").strip().lower() == "online (arp)":
            arp_only += int(n)
    return counts, arp_only


async def _sections(session: AsyncSession, visible: set[Any] | None, cfg: dict[str, Any],
                    facts: list[sn.SubnetFacts]) -> int:
    """區段數：看得到全部而且沒設顯示範圍＝全部區段（與儀表板一致）；否則＝範圍內子網路所屬的區段數。"""
    if visible is None and not scope_active(cfg):
        from app.models.section import Section
        return int(await session.scalar(select(func.count()).select_from(Section)) or 0)
    return len({f.section_id for f in facts if f.section_id})


async def _audit_24h(session: AsyncSession, cfg: dict[str, Any], ids: list[Any], now: datetime) -> int:
    from app.models.audit import AuditLog

    stmt = select(func.count()).select_from(AuditLog).where(AuditLog.ts >= now - AUDIT_WINDOW)
    if scope_active(cfg):
        if not ids:
            return 0
        stmt = stmt.where(or_(
            (AuditLog.object_type == "subnet") & in_values(AuditLog.object_id, ids),
            AuditLog.object_type.in_(IP_OBJECT_TYPES)
            & AuditLog.object_id.in_(select(IPAddress.id).where(in_values(IPAddress.subnet_id, ids))),
        ))
    return int(await session.scalar(stmt) or 0)


async def build(session: AsyncSession, *, user: Any, cfg: dict[str, Any], page: int = 1,
                now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    visible = await scoped_visibility(session, user, cfg)
    facts = await sn.load(session, visible, now=now)
    ids = [f.id for f in facts]
    registered = sum(f.used for f in facts)

    counts, arp_only = await _status_counts(session, ids)
    unit = cell_unit(max(counts.values()) if counts else 0)
    status = {
        "state": "ok" if registered else "empty", "total": registered, "unit": unit,
        "row_cells": ROW_CELLS, "row_cols": ROW_COLS, "online_arp_only": arp_only,
        "rows": [{"status": s, "count": counts[s], "cells": cells_for(counts[s], unit),
                  "pct": round(counts[s] * 100 / registered, 1) if registered else None} for s in STATUSES],
    }

    ranked = sorted((f for f in facts if f.used > 0), key=lambda f: (-f.used, f.cidr))
    shown, page, pages = paginate(ranked, PER_PAGE, page)
    ranking = section("ok" if ranked else "empty", total=len(ranked), items=[{
        "id": str(f.id), "rank": (page - 1) * PER_PAGE + i + 1, "label": f.label, "cidr": f.cidr,
        "version": f.version, "count": f.used,
        "pct": round(f.used * 100 / registered, 1) if registered else None,
    } for i, f in enumerate(shown)])

    families = {"v4": sum(f.used for f in facts if f.version == 4), "v6": sum(f.used for f in facts if f.version == 6)}
    audit = await _audit_24h(session, cfg, ids, now) if await full_access(session, user) else None
    return {
        "key": "stats", "data_at": now.isoformat(), "page": page, "pages": pages, "per_page": PER_PAGE,
        "total": len(ranked),
        "metrics": {
            "registered_ips": registered,
            "subnets": len(facts),
            "sections": await _sections(session, visible, cfg, facts),
            "audit_24h": audit,                       # None＝無權限（不是 0）
        },
        "status": status,
        "ranking": ranking,
        "families": families,
        "definitions": {
            "scope": "scoped" if scope_active(cfg) else "all" if visible is None else "visible",
            "audit_window_hours": int(AUDIT_WINDOW.total_seconds() // 3600),
            "audit_scoped": scope_active(cfg),
        },
    }
