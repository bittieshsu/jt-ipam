"""05 異常與曝露：依類型與時間分群的事件矩陣、優先處理的事件、對外開放服務的證據等級。

資料來源是異常觀測（anomaly_observations，0224）：每一筆從出現到消失的期間。
- 時間窗是最近 SLOTS × SLOT_MINUTES（6 小時、每格 20 分鐘）；一列一個類型（還沒消失的多的在前，最多 MAX_ROWS 列）
- 每格的狀態（優先順序由上而下）：
  new       這段時間有新出現的
  active    有還沒消失的（之前就出現了）
  resolved  這段時間存在過、後來已經消失
  quiet     沒有事件
  nodata    觀測資料從 data_since 才開始，這格整段在那之前：不知道，不畫成「沒有事件」
- baseline（觀測開始時就已經存在，0225）：開始那格算「持續中」不算「新出現」，也不算進 6 小時內新出現；
  清單帶 baseline 讓畫面寫「觀測開始前」而不是假的首次出現時間
- 優先處理：還沒消失的，高嚴重度在前、再依出現時間新到舊；每頁 PER_PAGE 筆
- 全部是管理員限定的資料（與異常偵測頁同一條線）：非管理員回 forbidden、不帶數量
- 對外開放服務的證據等級（已確認／依名稱／依埠號推測）與對外開放服務頁同一份判定（surface_service）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly_observation import AnomalyObservation
from app.services.warroom.common import observation_in_subnets, paginate, scope_subnet_ids, section
from app.services.warroom.overview import HIGH

SLOTS = 18
SLOT_MINUTES = 20
MAX_ROWS = 5
PER_PAGE = 3


def _cell(obs: list[tuple[datetime, datetime | None, bool]], lo: datetime, hi: datetime) -> str:
    state = "quiet"
    for first, resolved, baseline in obs:
        if first >= hi or (resolved is not None and resolved <= lo):
            continue                                   # 這段時間不存在
        if lo <= first < hi and not baseline:
            return "new"
        if resolved is None:
            state = "active"
        elif state == "quiet":
            state = "resolved"
    return state


async def _timeline(session: AsyncSession, now: datetime, where: list[Any]) -> dict[str, Any]:
    start = now - timedelta(minutes=SLOTS * SLOT_MINUTES)
    since = await session.scalar(select(func.min(AnomalyObservation.first_seen_at)))
    rows = (await session.execute(
        select(AnomalyObservation.category, AnomalyObservation.first_seen_at, AnomalyObservation.resolved_at,
               AnomalyObservation.baseline)
        .where(AnomalyObservation.first_seen_at < now, *where,
               or_(AnomalyObservation.resolved_at.is_(None), AnomalyObservation.resolved_at > start)))).all()
    if not rows:
        return section("empty", slots=SLOTS, slot_minutes=SLOT_MINUTES, start=start.isoformat(),
                       data_since=since.isoformat() if since else None, rows=[], more=0)
    by: dict[str, list[tuple[datetime, datetime | None, bool]]] = {}
    for cat, first, resolved, baseline in rows:
        by.setdefault(cat, []).append((first, resolved, baseline))
    cats = sorted(by, key=lambda c: (-sum(1 for _, r, _b in by[c] if r is None), -len(by[c]), c))
    out = []
    for cat in cats[:MAX_ROWS]:
        cells = []
        for i in range(SLOTS):
            lo = start + timedelta(minutes=i * SLOT_MINUTES)
            hi = lo + timedelta(minutes=SLOT_MINUTES)
            cells.append("nodata" if since is not None and hi <= since else _cell(by[cat], lo, hi))
        out.append({"category": cat, "open": sum(1 for _, r, _b in by[cat] if r is None),
                    "seen": len(by[cat]), "cells": cells})
    return section("ok", slots=SLOTS, slot_minutes=SLOT_MINUTES, start=start.isoformat(),
                   data_since=since.isoformat() if since else None, rows=out, more=max(len(cats) - MAX_ROWS, 0))


async def _priority(session: AsyncSession, page: int, where: list[Any]) -> dict[str, Any]:
    obs = list((await session.execute(
        select(AnomalyObservation).where(AnomalyObservation.resolved_at.is_(None), *where)
        .order_by(AnomalyObservation.first_seen_at.desc()).limit(500))).scalars().all())
    items = [{"id": str(o.id), "category": o.category, "label": o.label or "", "ip": str(o.ip) if o.ip else None,
              "severity": "high" if o.category in HIGH else "medium", "first_seen_at": o.first_seen_at.isoformat(), "baseline": bool(o.baseline)}
             for o in obs]
    items.sort(key=lambda i: (i["severity"] != "high",))            # 穩定排序：同嚴重度維持新到舊
    shown, page, pages = paginate(items, PER_PAGE, page)
    # 判斷線索（MAC 廠牌、VM、DNS 名稱）：只查畫面上這幾筆（hints.py）
    from app.services.warroom.hints import ip_hints
    hints = await ip_hints(session, [it["ip"] for it in shown if it.get("ip")])
    for n, it in enumerate(shown):
        it["rank"] = (page - 1) * PER_PAGE + n + 1
        if it.get("ip") in hints:
            it["hints"] = hints[it["ip"]]
    total = int(await session.scalar(select(func.count()).select_from(AnomalyObservation).where(
        AnomalyObservation.resolved_at.is_(None), *where)) or 0)       # 清單只取最新 500 筆，總數另外算
    return section("ok" if items else "empty", items=shown, total=total, page=page, pages=pages)


async def _evidence(session: AsyncSession) -> dict[str, Any]:
    from app.services.fw_lookup import attack_surface
    from app.services.surface_service import LEVELS, enrich

    try:
        items = await enrich(session, await attack_surface(session))
    except Exception:
        return section("empty")
    counts = dict.fromkeys(LEVELS, 0)
    for it in items:
        if it.get("level") in counts:
            counts[it["level"]] += 1
    return section("ok" if items else "empty", counts=counts, total=len(items),
                   sensitive=sum(1 for it in items if it.get("sensitive")))


async def build(session: AsyncSession, *, user: Any, cfg: dict[str, Any], page: int = 1,
                now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    from app.services.warroom.common import full_access
    if not await full_access(session, user):
        return {"key": "anomalies", "data_at": now.isoformat(), "page": 1, "pages": 1, "metrics": None,
                "timeline": section("forbidden"), "priority": section("forbidden"), "evidence": section("forbidden")}
    # 顯示範圍：只算落在範圍內子網路的觀測（沒有 IP 的那幾類，例如失效 DNS 記錄，設了範圍就不列）
    scope = await scope_subnet_ids(session, cfg)
    from app.services.warroom.common import actionable_observation
    where = [actionable_observation(), *([observation_in_subnets(scope)] if scope is not None else [])]
    prio = await _priority(session, page, where)
    six = now - timedelta(minutes=SLOTS * SLOT_MINUTES)
    new_6h = int(await session.scalar(select(func.count()).select_from(AnomalyObservation).where(
        AnomalyObservation.first_seen_at >= six, AnomalyObservation.baseline.is_(False), *where)) or 0)
    resolved_6h = int(await session.scalar(select(func.count()).select_from(AnomalyObservation).where(
        AnomalyObservation.resolved_at >= six, *where)) or 0)
    return {
        "key": "anomalies", "data_at": now.isoformat(), "page": prio["page"], "pages": prio["pages"],
        "per_page": PER_PAGE, "total": prio["total"],
        "metrics": {"open_anomalies": prio["total"], "new_6h": new_6h, "resolved_6h": resolved_6h},
        "timeline": await _timeline(session, now, where), "priority": prio, "evidence": await _evidence(session),
    }
