"""讀取時的權限（規格 §11.1）：每次看 run、證據、AI 解說、匯出都重新檢查。

分析當下的可見範圍算成 `visible_scope_hash` 存在 run 上；之後讀取時範圍變了：
- 看不到的發現與證據直接不回（不留名稱、不留數量）
- AI 解說若是用不同範圍產生的，整份隱藏（不能只遮 id 卻留下名稱與數量）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.change_impact.model import stable_hash

_TYPES = ("ip", "subnet", "device")


@dataclass
class Viewer:
    user: Any
    is_admin: bool
    global_read: bool
    vis: dict[str, set[uuid.UUID] | None]

    def can(self, vtype: str | None, vid: uuid.UUID | None) -> bool:
        if vtype in (None, "global"):
            return self.global_read
        if vtype == "admin":
            return self.is_admin
        v = self.vis.get(vtype)
        return v is None or (vid is not None and vid in v)

    def scope_hash(self) -> str:
        return stable_hash({"admin": self.is_admin, "global": self.global_read,
                            "vis": {k: (None if v is None else sorted(str(x) for x in v)) for k, v in self.vis.items()}})


async def viewer(session: AsyncSession, user: Any) -> Viewer:
    from app.mcp.tools import has_global_read
    from app.services.permission import visible_ids
    vis = {t: await visible_ids(session, user=user, object_type=t) for t in _TYPES}  # type: ignore[arg-type]
    return Viewer(user=user, is_admin=bool(getattr(user, "is_admin", False)),
                  global_read=await has_global_read(session, user), vis=vis)


def visible_manifest(manifest: dict[str, Any] | None, v: Viewer) -> dict[str, Any]:
    """資料來源清單也依讀者過濾：看不到監控／憑證（管理員限定）或全域類別的人，不可以從清單知道有哪些整合。"""
    from app.services.change_impact.sources import ADMIN_CATEGORIES, GLOBAL_CATEGORIES

    def ok(cats: list[str]) -> bool:
        return not any((c in ADMIN_CATEGORIES and not v.is_admin) or (c in GLOBAL_CATEGORIES and not v.global_read)
                       for c in cats)
    m = dict(manifest or {})
    m["sources"] = [s for s in m.get("sources") or [] if ok(list(s.get("categories") or []))]
    return m


def visible_counts(bundle: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any]:
    """依讀者看得到的發現／證據／資料不足重算數量（欄位與 engine 存的 counts 相同）。"""
    fs = bundle["findings"]
    return {
        "findings": len(fs),
        "blockers": sum(1 for f in fs if f.disposition == "blocker"),
        "review": sum(1 for f in fs if f.disposition == "review"),
        "informational": sum(1 for f in fs if f.disposition == "informational"),
        "change_required": sum(1 for f in fs if f.impact == "change_required"),
        "potential_disruption": sum(1 for f in fs if f.impact == "potential_disruption"),
        "evidence": len(bundle["evidence"]), "gaps": len(bundle["gaps"]),
        "roots": (stored or {}).get("roots"),
    }


def same_scope(run: Any, v: Viewer) -> bool:
    return not run.visible_scope_hash or run.visible_scope_hash == v.scope_hash()


async def visible_task_findings(session: AsyncSession, tasks: list[Any], v: Viewer) -> dict[uuid.UUID, list[str]]:
    """模板待辦帶的發現 id 依讀者過濾：task id → 看得到的 id。全部看不到的待辦不回（連類別名稱都不留）。"""
    from sqlalchemy import select

    from app.core.sqlin import in_values
    from app.models.change_impact import ImpactFinding
    wanted = {i for t in tasks for i in (t.finding_ids or [])}
    seen: set[str] = set()
    if wanted:
        rows = (await session.execute(select(ImpactFinding).where(
            in_values(ImpactFinding.id, [uuid.UUID(i) for i in wanted])))).scalars()
        seen = {str(f.id) for f in rows if v.can(f.visibility_type, f.visibility_id)}
    return {t.id: [i for i in (t.finding_ids or []) if i in seen] for t in tasks}
