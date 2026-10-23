"""07 採集健康：掃描代理（每頁四台）、整合來源（最近一次備份 10-22 依使用者要求拿掉）。

- 條帶是最近 CYCLES 輪（scan_agent_cycles，保留 7 天）：一格一輪，值是耗時／週期比（≥0.9 超載、≥0.5 忙碌，
  與負載面板同一個門檻）。少於 CYCLES 輪時前面補「無資料」格，不畫成正常
- 逾時（超過 STALE_AFTER 沒回報）的代理：條帶照實顯示最後幾輪，但標「資料逾時」，不顯示像是即時的執行時間
- 掃描代理、整合狀態都是管理員限定的資料（各自的管理頁也是）：非管理員回 forbidden
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scan_agent import ScanAgent
from app.models.scan_agent_cycle import ScanAgentCycle
from app.services.scan_load import BUSY_RATIO, OVERLOAD_RATIO
from app.services.warroom.common import paginate, section
from app.services.warroom.subnets import STALE_AFTER

PER_PAGE = 4
CYCLES = 24


def _agent_state(a: ScanAgent, ratio: float | None, now: datetime) -> str:
    if a.last_seen_at is None:
        return "never"
    if now - a.last_seen_at > STALE_AFTER:
        return "stale"
    if ratio is not None and ratio >= OVERLOAD_RATIO:
        return "overloaded"
    if ratio is not None and ratio >= BUSY_RATIO:
        return "busy"
    return "ok"


async def _agents(session: AsyncSession, page: int, now: datetime) -> tuple[dict[str, Any], dict[str, int]]:
    agents = list((await session.execute(select(ScanAgent).order_by(ScanAgent.name))).scalars().all())
    rows: list[dict[str, Any]] = []
    counts = {"ok": 0, "busy": 0, "overloaded": 0, "stale": 0, "never": 0}
    for a in agents:
        cyc = list((await session.execute(
            select(ScanAgentCycle.at, ScanAgentCycle.duration_s, ScanAgentCycle.interval_s)
            .where(ScanAgentCycle.agent_id == a.id).order_by(ScanAgentCycle.at.desc()).limit(CYCLES))).all())
        cyc.reverse()
        strip: list[dict[str, Any]] = [{"state": "none"}] * (CYCLES - len(cyc))
        for at, dur, itv in cyc:
            r = round(float(dur or 0) / max(int(itv or 0), 1), 3)
            strip.append({"state": "overloaded" if r >= OVERLOAD_RATIO else "busy" if r >= BUSY_RATIO else "ok",
                          "ratio": r, "at": at.isoformat()})
        last = cyc[-1] if cyc else None
        ratio = round(float(last[1] or 0) / max(int(last[2] or 0), 1), 3) if last else None
        state = _agent_state(a, ratio, now)
        counts[state] += 1
        rows.append({
            "id": str(a.id), "name": a.name, "description": getattr(a, "description", None),
            "version": a.agent_version, "state": state,
            "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
            "last_seen_s": int((now - a.last_seen_at).total_seconds()) if a.last_seen_at else None,
            "duration_s": round(float(last[1]), 1) if last and last[1] is not None else None,
            "interval_s": int(last[2]) if last and last[2] else None,
            "last_success_at": last[0].isoformat() if last else None,
            "strip": strip,
        })
    rank = {"stale": 0, "never": 1, "overloaded": 2, "busy": 3, "ok": 4}
    rows.sort(key=lambda r: (rank[r["state"]], r["name"]))
    shown, page, pages = paginate(rows, PER_PAGE, page)
    return section("ok" if rows else "empty", items=shown, total=len(rows), page=page, pages=pages), counts


MAX_SOURCES = 24


async def _integrations(session: AsyncSession, now: datetime) -> dict[str, Any]:
    from app.services.integration_registry import instances

    rows = [r for r in await instances(session, now=now) if r["state"] != "disabled"]
    rank = {"error": 0, "stale": 1, "never": 2, "ok": 3}
    rows.sort(key=lambda r: (rank.get(r["state"], 9), r["label"], r["name"]))
    for r in rows:
        r["last_sync_at"] = r["last_sync_at"].isoformat() if r["last_sync_at"] else None
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    # 全部列出（10-22 使用者：「整合來源沒顯示所有來源」）；上限只防極端數量把頁尾撐爆
    return section("ok" if rows else "empty", items=rows[:MAX_SOURCES], total=len(rows), counts=counts)


async def build(session: AsyncSession, *, user: Any, cfg: dict[str, Any], page: int = 1,
                now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    from app.services.warroom.common import full_access
    if not await full_access(session, user):
        return {"key": "collection", "data_at": now.isoformat(), "page": 1, "pages": 1,
                "metrics": None, "agents": section("forbidden"), "integrations": section("forbidden")}
    agents, counts = await _agents(session, page, now)
    integ = await _integrations(session, now)
    return {
        "key": "collection", "data_at": now.isoformat(), "page": agents.get("page", 1), "pages": agents.get("pages", 1),
        "per_page": PER_PAGE, "total": agents.get("total", 0),
        "metrics": {"agents_ok": counts["ok"] + counts["busy"], "agents_stale": counts["stale"] + counts["never"],
                    "agents_overloaded": counts["overloaded"],
                    "integrations_failing": integ.get("counts", {}).get("error", 0)},
        "agents": agents, "integrations": integ,
    }
