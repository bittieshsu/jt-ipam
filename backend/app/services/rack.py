"""機櫃 U 位放置的防呆驗證（共用給 device 建立/更新、rack 改 U 高）。

規則：
- 裝置 U 位不可越界（1 ≤ u_position，且 u_position + u_size - 1 ≤ rack.u_height）
- 同一機櫃、同一安裝方向（front/rear）內，U 區間不可與其他裝置重疊
- 縮小機櫃 U 高時，不可低於既有裝置的最高 U（否則那台會越界）

失敗時 raise RackPlacementError（人讀訊息），endpoint 翻成 HTTP 400/409。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ui_error import UiError
from app.models.device import Device
from app.models.location import Rack


class RackPlacementError(UiError, ValueError):
    """U 位放置不合法（越界 / 重疊 / 縮櫃衝突）。"""


# 一個 U（層架則是一層）的橫向分格數。**60 = 1~6 的最小公倍數**，所以 1/2、1/3、1/4、
# **1/5**、1/6 都表達得出來。
#
# 一開始取 12（只看 issue #31 要的 6 等分），但 issue #30 的層架明講「一層可以並排 3~5 台」
# —— 12 除不盡 5。格數只是個常數、不影響成本，所以取能同時滿足兩個 issue 的 60。
RACK_SLOTS = 60

# 使用者在介面上選的「寬度」→ 跨幾格。分母必須整除 RACK_SLOTS。
RACK_WIDTH_PARTS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)


def slots_for(parts: int) -> int:
    """寬度 1/parts 佔幾格（parts=1 即整個 U）。"""
    if parts not in RACK_WIDTH_PARTS:
        raise ValueError(f"unsupported width 1/{parts}")
    return RACK_SLOTS // parts


# ── 立面圖的尺寸（issue #30：非標準寬度/層高的層架要畫得對） ─────────────────
# 基準＝標準 19" 機櫃：寬 482.6mm 畫成 250px、1U 44.45mm 畫成 28px。非標準的依比例縮放，
# 但夾在合理範圍內 —— 完全照實體比例，一個 350mm 高的層架單層就會有 220px，整張圖看不完。
RACK_REF_WIDTH_MM = 482.6      # 19"
RACK_REF_ROW_MM = 44.45        # 1U
RACK_REF_WIDTH_PX = 250.0
RACK_REF_ROW_PX = 28.0
# 機架型態（issue #30）。前兩種以 U 計、後兩種以「層」計：
#   rack        標準伺服器機櫃（19"）
#   industrial  工業機櫃（箱體較寬，仍是 U 制）
#   shelf       一般層架（鋼板／木板層板）
#   wire_shelf  鍍鉻層架（圓管立柱 + 網狀層板，中小企業很常見）
#   wood_shelf  木質層架（松木側架 + 層板，如 IKEA IVAR）
RACK_KINDS: tuple[str, ...] = ("rack", "industrial", "shelf", "wire_shelf", "wood_shelf")
_U_KINDS = frozenset({"rack", "industrial"})

# 預設尺寸。木質層架取 IKEA IVAR 最小的那組：層板 42×30 公分、側架高 179 公分，
# 官方要求 179 的側架至少配 4 層，實務上多半放 6 層 → 每層約 300mm。
_DEFAULT_WIDTH_MM = {"rack": 482.6, "industrial": 600, "shelf": 900,
                     "wire_shelf": 1200, "wood_shelf": 420}
_DEFAULT_ROW_MM = {"rack": 44.45, "industrial": 44.45, "shelf": 350,
                   "wire_shelf": 400, "wood_shelf": 300}
# 層板本身的厚度（mm）。層高填的是**淨空高**（不含板），所以總高要另外把板算進去。
# 木質層架 18mm 是 IKEA IVAR 層板的官方規格（1.8 cm）。
_DEFAULT_BOARD_MM = {"rack": 0.0, "industrial": 0.0, "shelf": 20.0,
                     "wire_shelf": 35.0, "wood_shelf": 18.0}


def uses_rack_units(kind: str | None) -> bool:
    """這個型態的列是「U」還是「層」—— 畫面標籤與說明文字都看它。"""
    return (kind or "rack") in _U_KINDS


def rack_defaults(kind: str | None) -> tuple[float, float]:
    """(預設寬 mm, 預設列高 mm)。層架沒有標準值，給一組常見尺寸。"""
    k = kind if kind in _DEFAULT_WIDTH_MM else "rack"
    return float(_DEFAULT_WIDTH_MM[k]), float(_DEFAULT_ROW_MM[k])


def board_default_mm(kind: str | None) -> float:
    """層板厚度的預設值（mm）。機櫃類沒有層板，回 0。"""
    return float(_DEFAULT_BOARD_MM.get(kind or "rack", 0.0))


def has_open_top(kind: str | None) -> bool:
    """這種型態的**頂板上面**放不放得了東西。

    層架沒有天花板，最上面那片板的上面本來就是可以放設備的位置（IVAR、鍍鉻層架都是）；
    機櫃有頂蓋，放不了。多出來的那個位置在資料上是「第 u_height + 1 層」。
    """
    return not uses_rack_units(kind)


def placeable_levels(kind: str | None, u_height: int | None) -> int:
    """可以放裝置的位置有幾個。層架多一個：最上面那片板的上面。"""
    n = max(int(u_height or 0), 0)
    return n + 1 if (has_open_top(kind) and n) else n


# 列高的上限。76 是「不知道有幾列」時的保守值；知道列數時改用整張圖的高度預算
# （_TOTAL_MAX_PX）換算，因為真正該控制的是整張圖多高，不是單列多高 —— 一個 5 層
# 150 公分的層架每層 30 公分，卡在 76px 會被畫成矮胖的樣子，跟實物完全不像。
# 預算只會「放寬」上限、不會收緊（見下面的 max）：42U 機櫃仍然是 28px，既有的圖都不變。
_ROW_CAP_PX = 76.0
_ROW_CAP_MAX_PX = 160.0
_TOTAL_MAX_PX = 1200.0          # ≈ 42U 機櫃原本就會畫出來的高度


def level_heights_mm(kind: str | None, row_height_mm: int | None,
                     level_heights: list[int] | None, rows: int) -> list[float]:
    """每一層的實際高度（mm），由**第 1 層起算**（第 1 層＝編號最小的那層）。

    層架的層高本來就一層一層可以調（IVAR、鍍鉻層架都是），所以資料上存一個陣列。
    沒給、或長度對不上就用 `row_height_mm` 補滿 —— 既有資料一律走這條，畫出來不變。
    """
    n = max(int(rows or 0), 0)
    if n == 0:
        return []
    _, dr = rack_defaults(kind)
    uniform = float(row_height_mm or dr)
    out = [uniform] * n
    for i, h in enumerate(level_heights or []):
        if i >= n:
            break
        try:
            v = float(h)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out[i] = v
    return out


def rack_render_size(
    kind: str | None, width_mm: int | None, row_height_mm: int | None,
    rows: int | None = None,
) -> tuple[float, float]:
    """回 (畫出來的寬 px, 每列高 px)。標準機櫃會得到與改版前完全相同的 250/28。

    `rows` 給了就用整張圖的高度預算決定列高上限，層架才畫得出真實比例。
    """
    dw, dr = rack_defaults(kind)
    w = float(width_mm or dw)
    r = float(row_height_mm or dr)
    px_w = RACK_REF_WIDTH_PX * (w / RACK_REF_WIDTH_MM)
    px_r = RACK_REF_ROW_PX * (r / RACK_REF_ROW_MM)
    cap = _ROW_CAP_PX
    if rows:
        cap = max(cap, min(_ROW_CAP_MAX_PX, _TOTAL_MAX_PX / max(int(rows), 1)))
    return (min(max(px_w, 180.0), 620.0), min(max(px_r, 18.0), cap))


# IKEA OBSERVATÖR 交叉支撐桿：固定 100 公分的鍍鋅鋼條，一包一支（是一根斜桿，不是 X）。
# 因為長度固定，它能跨幾層完全由層架寬度決定 —— 斜邊固定，底邊越寬、垂直跨距就越短。
# 官方數據可以對得起來：83 公分寬的配置垂直跨距約 21 吋（53 公分）、
# 42 公分寬的約 35.5 吋（90 公分），跟 √(100² − 寬²) 幾乎一樣。
OBSERVATOR_LEN_MM = 1000.0


def brace_span_mm(width_mm: float) -> float:
    """支撐桿的垂直跨距（mm）。寬到 100 公分以上就跨不了，回 0。"""
    w = float(width_mm or 0)
    if w <= 0 or w >= OBSERVATOR_LEN_MM:
        return 0.0
    return (OBSERVATOR_LEN_MM ** 2 - w ** 2) ** 0.5


def brace_levels(kind: str | None, width_mm: int | None,
                 row_height_mm: int | None, rows: int | None,
                 level_heights: list[int] | None = None) -> int:
    """支撐桿從最底下那層往上**實際跨得到**幾層。非木質層架、或跨不了，回 0（＝不畫）。

    逐層累加而不是除法：層高可以一層一層不同，除法只在均一時才成立。
    累加到超過鋼條長度就停 —— 跨過頭的話實物根本裝不上去。
    """
    if (kind or "") != "wood_shelf":
        return 0
    dw, _ = rack_defaults(kind)
    span = brace_span_mm(float(width_mm or dw))
    if span <= 0:
        return 0
    acc = 0.0
    n = 0
    for h in level_heights_mm(kind, row_height_mm, level_heights, int(rows or 0)):
        if acc + h > span:
            break
        acc += h
        n += 1
    return n


def legacy_side_to_slots(side: str | None) -> tuple[int, int]:
    """舊的 full/left/right（含舊版系統匯出檔）→ (起始格, 跨幾格)。認不得一律視為整 U。"""
    if side == "left":
        return (0, RACK_SLOTS // 2)
    if side == "right":
        return (RACK_SLOTS // 2, RACK_SLOTS // 2)
    return (0, RACK_SLOTS)


def _face(v: str | None) -> str:
    return v or "front"


def _slot_range(slot: int | None, span: int | None) -> range:
    """把 (起始格, 跨幾格) 正規化成半開區間；None 視為佔滿整個 U。"""
    s = 0 if slot is None else int(slot)
    n = RACK_SLOTS if span is None else int(span)
    return range(s, s + n)


async def assert_placement_ok(
    session: AsyncSession,
    *,
    rack_id: uuid.UUID,
    u_position: int,
    u_size: int,
    rack_face: str | None,
    rack_slot: int | None = None,
    rack_slot_span: int | None = None,
    rack_vslot: int | None = None,
    rack_vslot_span: int | None = None,
    exclude_device_id: uuid.UUID | None = None,
) -> None:
    """驗證某裝置放在 rack_id 的 [u_position, u_position+u_size-1] 與格位是否合法。

    重疊 = **U 區間相交 且 橫向格位相交 且 層內垂直格位相交**（前後面不同不算重疊）。
    層內垂直格位是層架才有的彈性：一層放得下疊起來的兩三台，而且不必放滿。
    """
    if u_size < 1:
        raise RackPlacementError("u_size 必須 ≥ 1", code="rack_u_size_min")
    want_slots = _slot_range(rack_slot, rack_slot_span)
    if len(want_slots) < 1:
        raise RackPlacementError("橫向跨度必須 ≥ 1 格", code="rack_slot_span_min")
    if want_slots.start < 0 or want_slots.stop > RACK_SLOTS:
        raise RackPlacementError(
            f"橫向格位超出範圍：一個 U 只有 {RACK_SLOTS} 格",
            code="rack_slot_out_of_range", slots=RACK_SLOTS,
        )
    want_v = _slot_range(rack_vslot, rack_vslot_span)
    if len(want_v) < 1:
        raise RackPlacementError("層內垂直跨度必須 ≥ 1 格", code="rack_vslot_span_min")
    if want_v.start < 0 or want_v.stop > RACK_SLOTS:
        raise RackPlacementError(
            f"層內垂直格位超出範圍：一層只有 {RACK_SLOTS} 格",
            code="rack_vslot_out_of_range", slots=RACK_SLOTS,
        )
    rack = await session.get(Rack, rack_id)
    if rack is None:
        raise RackPlacementError("機櫃不存在", code="rack_not_found")

    top = u_position + u_size - 1
    # 層架多一個可放的位置：最上面那片板的**上面**（＝第 u_height + 1 層）。
    limit = placeable_levels(getattr(rack, "kind", None), rack.u_height)
    if u_position < 1 or top > limit:
        raise RackPlacementError(
            f"U 位超出機櫃範圍：裝置占 U{u_position}–U{top}，"
            f"但「{rack.name}」只有 {rack.u_height}U",
            code="rack_out_of_range", bottom=u_position, top=top,
            name=rack.name, height=rack.u_height,
        )

    face = _face(rack_face)
    others = (await session.execute(
        select(Device).where(
            Device.rack_id == rack_id,
            Device.u_position.is_not(None),
            Device.u_size.is_not(None),
        )
    )).scalars().all()
    want = set(range(u_position, top + 1))
    for d in others:
        if exclude_device_id is not None and d.id == exclude_device_id:
            continue
        if _face(d.rack_face) != face:
            continue  # 不同安裝方向（前/後）不算重疊
        if d.u_position is None or d.u_size is None:
            continue
        d_slots = _slot_range(d.rack_slot, d.rack_slot_span)
        if not (want_slots.start < d_slots.stop and d_slots.start < want_slots.stop):
            continue  # 橫向格位沒交集（並排），同 U 不算重疊
        d_v = _slot_range(getattr(d, "rack_vslot", 0), getattr(d, "rack_vslot_span", RACK_SLOTS))
        if not (want_v.start < d_v.stop and d_v.start < want_v.stop):
            continue  # 層內垂直格位沒交集（上下疊放），同一層不算重疊
        d_range = set(range(d.u_position, d.u_position + d.u_size))
        clash = sorted(want & d_range)
        if clash:
            us = ", ".join(f"U{u}" for u in clash)
            raise RackPlacementError(
                f"與「{d.name}」的 U 位重疊（{us}）；請改放空的 U 位或調整 U 數",
                code="rack_overlap", name=d.name, positions=us,
            )


async def assert_rack_height_ok(
    session: AsyncSession, *, rack_id: uuid.UUID, new_height: int,
) -> None:
    """縮小機櫃 U 高前，確認不會把既有裝置擠到範圍外。"""
    rows = (await session.execute(
        select(Device.name, Device.u_position, Device.u_size).where(
            Device.rack_id == rack_id,
            Device.u_position.is_not(None),
            Device.u_size.is_not(None),
        )
    )).all()
    offenders = [
        (name, pos, size) for (name, pos, size) in rows
        if pos is not None and size is not None and (pos + size - 1) > new_height
    ]
    if offenders:
        names = "、".join(
            f"{n}(U{p}–U{p + s - 1})" for (n, p, s) in offenders[:5]
        )
        raise RackPlacementError(
            f"無法縮小到 {new_height}U：以下裝置會超出範圍 → {names}。"
            "請先移走或下移這些裝置。",
            code="rack_shrink_blocked", height=new_height, devices=names,
        )


def level_render_px(
    kind: str | None, width_mm: int | None, row_height_mm: int | None,
    level_heights: list[int] | None, rows: int | None,
) -> tuple[float, list[float]]:
    """回 (寬 px, 每一層的高 px)。每層各自換算，所以高的層畫得高、矮的層畫得矮。

    夾住的方式與 `rack_render_size` 一致（同一個高度預算），差別只在逐層算。
    回傳的順序與 `level_heights_mm` 相同：由第 1 層起算。
    """
    n = max(int(rows or 0), 0)
    px_w, _ = rack_render_size(kind, width_mm, row_height_mm, n)
    if n == 0:
        return px_w, []
    mm = level_heights_mm(kind, row_height_mm, level_heights, n)
    cap = max(_ROW_CAP_PX, min(_ROW_CAP_MAX_PX, _TOTAL_MAX_PX / n))
    px = [min(max(RACK_REF_ROW_PX * (m / RACK_REF_ROW_MM), 18.0), cap) for m in mm]
    return px_w, px
