"""戰情室共用：方格分配、彙整單位、分頁、可見範圍。

方格（規範「資料正確性」）：
- 一格一個物件時照物件本身的順序排（位址順序有意義）
- 物件太多時一格代表 N 個（N 取 2 的次方），依狀態排序後按比例分格（鬆餅圖）；畫面要標「每格約 N 個」，
  彙整格不冒充單一物件。有值的狀態至少一格（一筆異常不會因為四捨五入消失），所以彙整時格數是近似值，
  精確數字看旁邊的計數。
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

#: 一個區塊最多幾格（2×2 版面每區 16×16）
MAX_CELLS = 256


def unit_for(total: int, max_cells: int = MAX_CELLS) -> int:
    """一格代表幾個物件：1、2、4、8…，讓格數不超過 max_cells。"""
    unit = 1
    while math.ceil(total / unit) > max_cells:
        unit *= 2
    return unit


def waffle(counts: list[tuple[str, int]], cells: int) -> list[str]:
    """依各狀態的數量分配 cells 格（依 counts 的順序排），最大餘數法；有值的狀態至少一格。"""
    total = sum(n for _, n in counts)
    if cells <= 0 or total <= 0:
        return []
    nonzero = [(s, n) for s, n in counts if n > 0]
    if cells <= len(nonzero):              # 格子比狀態還少：每個有值的狀態一格，多的截掉
        return [s for s, _ in nonzero][:cells]
    exact = [(s, n * cells / total) for s, n in nonzero]
    alloc = {s: max(1, math.floor(x)) for s, x in exact}
    diff = cells - sum(alloc.values())
    order = sorted(exact, key=lambda sx: sx[1] - math.floor(sx[1]), reverse=diff > 0)
    i = 0
    while diff != 0 and order:
        s = order[i % len(order)][0]
        if diff > 0:
            alloc[s] += 1
            diff -= 1
        elif alloc[s] > 1:
            alloc[s] -= 1
            diff += 1
        i += 1
        if i > 10 * len(order) * max(1, abs(diff)):
            break
    out: list[str] = []
    for s, _ in counts:
        out.extend([s] * alloc.get(s, 0))
    return out


def paginate(items: list[Any], per_page: int, page: int) -> tuple[list[Any], int, int]:
    """(這一頁的項目, 第幾頁（從 1 起，超出範圍會夾回來）, 共幾頁)。沒有項目時是 ([], 1, 1)。"""
    per_page = max(1, per_page)
    pages = max(1, math.ceil(len(items) / per_page))
    page = min(max(1, page), pages)
    return items[(page - 1) * per_page: page * per_page], page, pages


async def subnet_visibility(session: AsyncSession, user: Any) -> set[Any] | None:
    """None＝全部看得到；否則是看得到的子網路 id。"""
    from app.services.permission import visible_ids
    return await visible_ids(session, user=user, object_type="subnet")


async def full_access(session: AsyncSession, user: Any) -> bool:
    """戰情室裡管理員限定的段落看不看得到：管理員，或屬於內建角色「戰情室檢視者」。
    只用在戰情室（唯讀看板）；異常偵測、憑證等頁面與 REST／MCP 不看這個。"""
    if getattr(user, "is_admin", False):
        return True
    from sqlalchemy import select

    from app.models.user import Group, UserGroupMember
    from app.services.permission import WARROOM_ROLE
    return bool((await session.execute(
        select(UserGroupMember.user_id).join(Group, Group.id == UserGroupMember.group_id)
        .where(UserGroupMember.user_id == user.id, Group.name == WARROOM_ROLE, Group.is_builtin.is_(True))
        .limit(1))).first())


def section(state: str, **data: Any) -> dict[str, Any]:
    """一段資料：state 是 ok／empty／forbidden／disabled，畫面依此顯示不同的狀態（無權限不帶數量）。"""
    return {"state": state, **data}


# ─────────────────────── 顯示範圍（戰情室設定，2026-10-21 使用者要求） ───────────────────────
#
# 管理員可以指定戰情室只顯示某些單位、區段或子網路（三者取聯集；選區段含底下的子區段；
# 單位看子網路自己的單位或所屬區段的單位）。三個都沒選＝全部。
# 顯示範圍只會縮小，不會放大：最後看到的是「觀看者的權限範圍 ∩ 顯示範圍」。

SCOPE_KINDS = ("customers", "sections", "subnets")


def scope_active(cfg: dict[str, Any]) -> bool:
    sc = cfg.get("scope") or {}
    return any(sc.get(k) for k in SCOPE_KINDS)


async def scope_subnet_ids(session: AsyncSession, cfg: dict[str, Any]) -> set[Any] | None:
    """顯示範圍內的子網路 id；None＝沒有設定範圍（全部）。"""
    import uuid

    from sqlalchemy import or_, select

    from app.core.sqlin import in_values
    from app.models.section import Section
    from app.models.subnet import Subnet

    if not scope_active(cfg):
        return None
    sc = cfg["scope"]
    as_ids = lambda xs: [uuid.UUID(str(x)) for x in xs]  # noqa: E731
    customers, sections, subnets = as_ids(sc.get("customers") or []), as_ids(sc.get("sections") or []), \
        as_ids(sc.get("subnets") or [])
    sec_ids: set[Any] = set()
    if sections or customers:
        rows = (await session.execute(select(Section.id, Section.parent_id, Section.customer_id))).all()
        children: dict[Any, list[Any]] = {}
        for sid, parent, _c in rows:
            children.setdefault(parent, []).append(sid)
        todo = list(sections) + [sid for sid, _p, c in rows if c is not None and c in set(customers)]
        while todo:                                   # 區段含底下所有子區段
            sid = todo.pop()
            if sid in sec_ids:
                continue
            sec_ids.add(sid)
            todo.extend(children.get(sid, []))
    conds = []
    if subnets:
        conds.append(in_values(Subnet.id, subnets))
    if sec_ids:
        conds.append(in_values(Subnet.section_id, sec_ids))
    if customers:
        conds.append(in_values(Subnet.customer_id, customers))
    return {r[0] for r in (await session.execute(select(Subnet.id).where(or_(*conds))))}


async def scoped_visibility(session: AsyncSession, user: Any, cfg: dict[str, Any]) -> set[Any] | None:
    """觀看者看得到、而且在顯示範圍內的子網路 id；None＝全部。"""
    visible = await subnet_visibility(session, user)
    scope = await scope_subnet_ids(session, cfg)
    if scope is None:
        return visible
    return scope if visible is None else (scope & set(visible))


def observation_in_subnets(ids: set[Any]) -> Any:
    """異常觀測落在這些子網路：有 subnet_id 的直接比；只有 IP 的看哪個子網路包含它。"""
    from sqlalchemy import exists, or_, select

    from app.core.sqlin import in_values
    from app.models.anomaly_observation import AnomalyObservation
    from app.models.subnet import Subnet

    ids = list(ids)
    return or_(in_values(AnomalyObservation.subnet_id, ids),
               exists(select(Subnet.id).where(in_values(Subnet.id, ids),
                                              Subnet.cidr.op(">>=")(AnomalyObservation.ip))))


#: 異常偵測裡「只供參考、不通知」的類別（例行的 MAC 換埠：VM 遷移、無線漫遊）：戰情室不列、不計數
#: （10-23 使用者看到時序裡多一列沒翻譯的 mac_drift_reference，問「為何」）
REFERENCE_CATEGORIES = ("mac_drift_reference",)


def actionable_observation() -> Any:
    from app.models.anomaly_observation import AnomalyObservation
    return AnomalyObservation.category.not_in(REFERENCE_CATEGORIES)

