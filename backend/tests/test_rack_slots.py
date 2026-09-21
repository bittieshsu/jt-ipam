"""同一個 U 裡的橫向分格（GitHub issue #31）。

由來：原本寬度只有 full / left / right 三種，客戶的機櫃放不下 —— 一個 U 裡可能並排
更多台。改成「起始格 + 跨幾格」的區間模型，建立在 `RACK_SLOTS` 格的網格上。

為什麼是 60 格：60 是 1~6 的最小公倍數，所以 1/2、1/3、1/4、**1/5**、1/6 都表達得出來。
先前選 12 只看了 issue #31（要 6 等分），但 issue #30 的層架明講「一層可以並排 3~5 台」——
12 除不盡 5。格數只是個常數，成本一樣，所以取能同時滿足兩者的 60。

重點：重疊判定變成「U 區間相交 **且** 格子區間相交」—— 跟原本對 U 位做的是同一種運算，
不是另一套機制。
"""

from __future__ import annotations

import pytest
from app.services.rack import (
    RACK_SLOTS,
    RackPlacementError,
    assert_placement_ok,
    slots_for,
)


async def _mk_rack(session, *, u_height=18, name="R"):  # type: ignore[no-untyped-def]
    from app.models.location import Rack
    rk = Rack(name=name, u_height=u_height)
    session.add(rk)
    await session.flush()
    return rk


async def _mk_device(session, *, rack_id, u_position, u_size=1, face=None,
                     slot=0, span=RACK_SLOTS, name="d"):  # type: ignore[no-untyped-def]
    from app.models.device import Device
    d = Device(name=name, type="server", rack_id=rack_id,
               u_position=u_position, u_size=u_size, rack_face=face,
               rack_slot=slot, rack_slot_span=span)
    session.add(d)
    await session.flush()
    return d


# ─────────────────── 網格常數與寬度換算 ───────────────────

def test_grid_divides_every_width_we_offer() -> None:
    """60 格的重點：1/2、1/3、1/4、1/5、1/6 都要整除（1/5 是 issue #30 的層架要的）。"""
    for parts in (1, 2, 3, 4, 5, 6):
        assert RACK_SLOTS % parts == 0, parts


def test_slots_for_maps_width_preset_to_span() -> None:
    assert slots_for(1) == RACK_SLOTS          # 整 U
    assert slots_for(2) == RACK_SLOTS // 2     # 1/2
    assert slots_for(3) == RACK_SLOTS // 3     # 1/3
    assert slots_for(4) == RACK_SLOTS // 4     # 1/4 —— 6 格做不到的那個
    assert slots_for(5) == RACK_SLOTS // 5     # 1/5 —— 12 格做不到的那個（issue #30 層架）
    assert slots_for(6) == RACK_SLOTS // 6     # 1/6


# ─────────────────── 重疊判定 ───────────────────

@pytest.mark.anyio
async def test_five_devices_fit_in_one_level(db_session, admin_user) -> None:
    """issue #30 的層架：一層要放得下 5 台（12 格時這個測試不可能過）。"""
    rk = await _mk_rack(db_session)
    span = slots_for(5)
    for i in range(5):
        await _mk_device(db_session, rack_id=rk.id, u_position=8,
                         slot=i * span, span=span, name=f"s{i}")
    with pytest.raises(RackPlacementError):
        await assert_placement_ok(db_session, rack_id=rk.id, u_position=8, u_size=1,
                                  rack_face=None, rack_slot=0, rack_slot_span=span)


@pytest.mark.anyio
async def test_six_devices_fit_in_one_u(db_session, admin_user) -> None:
    """issue #31 的核心訴求：一個 U 要放得下 6 台。"""
    rk = await _mk_rack(db_session)
    span = slots_for(6)
    for i in range(6):
        await _mk_device(db_session, rack_id=rk.id, u_position=5,
                         slot=i * span, span=span, name=f"d{i}")
    # 第 7 台再塞就一定撞到
    with pytest.raises(RackPlacementError):
        await assert_placement_ok(db_session, rack_id=rk.id, u_position=5, u_size=1,
                                  rack_face=None, rack_slot=0, rack_slot_span=span)


@pytest.mark.anyio
async def test_adjacent_slots_do_not_conflict(db_session, admin_user) -> None:
    """並排但不重疊的兩格不算衝突。"""
    rk = await _mk_rack(db_session)
    span = slots_for(4)
    await _mk_device(db_session, rack_id=rk.id, u_position=3, slot=0, span=span)
    await assert_placement_ok(db_session, rack_id=rk.id, u_position=3, u_size=1,
                              rack_face=None, rack_slot=span, rack_slot_span=span)


@pytest.mark.anyio
async def test_partial_slot_overlap_conflicts(db_session, admin_user) -> None:
    """只要格子區間有交集就算撞 —— 1/2 寬會吃掉它涵蓋的每個 1/6 格。"""
    rk = await _mk_rack(db_session)
    await _mk_device(db_session, rack_id=rk.id, u_position=7,
                     slot=0, span=slots_for(2))          # 左半
    with pytest.raises(RackPlacementError):              # 左半裡的某個 1/6
        await assert_placement_ok(db_session, rack_id=rk.id, u_position=7, u_size=1,
                                  rack_face=None, rack_slot=slots_for(6),
                                  rack_slot_span=slots_for(6))


@pytest.mark.anyio
async def test_slot_overlap_still_requires_u_overlap(db_session, admin_user) -> None:
    """格子相同但 U 不重疊 → 不衝突（兩個維度都要相交才算撞）。"""
    rk = await _mk_rack(db_session)
    await _mk_device(db_session, rack_id=rk.id, u_position=2, slot=0, span=slots_for(2))
    await assert_placement_ok(db_session, rack_id=rk.id, u_position=9, u_size=1,
                              rack_face=None, rack_slot=0, rack_slot_span=slots_for(2))


@pytest.mark.anyio
async def test_different_face_still_ignores_slots(db_session, admin_user) -> None:
    """前後面本來就不算重疊，加了分格也不該改變這件事。"""
    rk = await _mk_rack(db_session)
    await _mk_device(db_session, rack_id=rk.id, u_position=4, face="front",
                     slot=0, span=slots_for(6))
    await assert_placement_ok(db_session, rack_id=rk.id, u_position=4, u_size=1,
                              rack_face="rear", rack_slot=0, rack_slot_span=slots_for(6))


# ─────────────────── 防呆 ───────────────────

@pytest.mark.anyio
async def test_slot_out_of_grid_is_rejected(db_session, admin_user) -> None:
    rk = await _mk_rack(db_session)
    with pytest.raises(RackPlacementError):      # 起始格越界
        await assert_placement_ok(db_session, rack_id=rk.id, u_position=1, u_size=1,
                                  rack_face=None, rack_slot=RACK_SLOTS, rack_slot_span=1)
    with pytest.raises(RackPlacementError):      # 跨度超出右邊界
        await assert_placement_ok(db_session, rack_id=rk.id, u_position=1, u_size=1,
                                  rack_face=None, rack_slot=RACK_SLOTS - 1,
                                  rack_slot_span=2)
    with pytest.raises(RackPlacementError):      # 跨度必須 ≥ 1
        await assert_placement_ok(db_session, rack_id=rk.id, u_position=1, u_size=1,
                                  rack_face=None, rack_slot=0, rack_slot_span=0)


# ─────────────────── 舊資料相容 ───────────────────

def test_legacy_rack_side_maps_onto_the_grid() -> None:
    """舊的 full/left/right（含舊系統匯出檔）要能映射成 slot/span。"""
    from app.services.rack import legacy_side_to_slots
    assert legacy_side_to_slots("full") == (0, RACK_SLOTS)
    assert legacy_side_to_slots("left") == (0, RACK_SLOTS // 2)
    assert legacy_side_to_slots("right") == (RACK_SLOTS // 2, RACK_SLOTS // 2)
    assert legacy_side_to_slots(None) == (0, RACK_SLOTS)
    assert legacy_side_to_slots("nonsense") == (0, RACK_SLOTS)


def test_legacy_export_file_maps_rack_side_on_import() -> None:
    """舊版匯出檔只有 rack_side —— 匯入時要轉成格位，否則半 U 裝置會被還原成整 U 而互相重疊。"""
    from app.models.device import Device
    from app.services.system_transfer.importer import _coerce

    table = Device.__table__
    got = _coerce(table, {"name": "d", "rack_side": "right"})
    assert got["rack_slot"] == RACK_SLOTS // 2
    assert got["rack_slot_span"] == RACK_SLOTS // 2
    assert "rack_side" not in got          # 舊欄位不該被寫進新表

    # 新版檔案（已經有 rack_slot）不可被舊欄位覆蓋
    got = _coerce(table, {"name": "d", "rack_side": "left",
                          "rack_slot": 4, "rack_slot_span": 2})
    assert (got["rack_slot"], got["rack_slot_span"]) == (4, 2)


# ─────────────────── 層架尺寸（issue #30） ───────────────────

def test_standard_rack_renders_exactly_as_before() -> None:
    """標準機櫃（沒設尺寸）必須維持原本的 250×28 —— 加了層架支援不可以動到既有畫面。"""
    from app.services.rack import rack_render_size
    assert rack_render_size("rack", None, None) == (250.0, 28.0)
    assert rack_render_size(None, None, None) == (250.0, 28.0)


def test_shelf_is_wider_and_taller_than_a_rack() -> None:
    """層架比 19" 寬、單層也比 1U 高，否則三層架會被畫成三條細線。"""
    from app.services.rack import rack_render_size
    rw, rr = rack_render_size("rack", None, None)
    sw, sr = rack_render_size("shelf", None, None)
    assert sw > rw and sr > rr


def test_custom_size_scales_proportionally_but_is_clamped() -> None:
    from app.services.rack import RACK_REF_WIDTH_MM, rack_render_size
    # 兩倍寬 → 兩倍 px（未觸頂）
    w, _ = rack_render_size("rack", int(RACK_REF_WIDTH_MM * 2), None)
    assert abs(w - 500.0) < 1.0
    # 誇張的值要被夾住，不能把版面撐爆
    w2, r2 = rack_render_size("shelf", 5000, 5000)
    assert w2 <= 620.0 and r2 <= 76.0


# ─────────────────── 機架型態（標準／工業／層架／鍍鉻層架） ───────────────────

def test_all_rack_kinds_have_sensible_defaults() -> None:
    """四種型態各有合理的預設尺寸；U 制的兩種列高相同，層架類的明顯較高。"""
    from app.services.rack import RACK_KINDS, rack_defaults
    assert set(RACK_KINDS) == {"rack", "industrial", "shelf", "wire_shelf", "wood_shelf"}
    rack_w, rack_r = rack_defaults("rack")
    ind_w, ind_r = rack_defaults("industrial")
    sh_w, sh_r = rack_defaults("shelf")
    wire_w, wire_r = rack_defaults("wire_shelf")
    assert ind_r == rack_r            # 工業機櫃仍是 U 制，列高同標準
    assert ind_w > rack_w             # 但箱體通常比 19" 寬
    assert sh_r > rack_r and wire_r > rack_r     # 層架一層遠高於 1U
    assert wire_w >= sh_w             # 鍍鉻層架常見 120cm，比一般層架寬


def test_unknown_kind_falls_back_to_standard_rack() -> None:
    from app.services.rack import rack_defaults, rack_render_size
    assert rack_defaults("nonsense") == rack_defaults("rack")
    assert rack_render_size("nonsense", None, None) == (250.0, 28.0)


def test_kinds_that_use_u_vs_levels() -> None:
    """列的單位：機櫃類以 U 計、層架類以「層」計 —— 畫面標籤與說明都靠這個。"""
    from app.services.rack import uses_rack_units
    assert uses_rack_units("rack") and uses_rack_units("industrial")
    assert not uses_rack_units("shelf") and not uses_rack_units("wire_shelf")
    assert not uses_rack_units("wood_shelf")


@pytest.mark.anyio
async def test_every_kind_round_trips_through_the_database(db_session) -> None:
    """每一種型態都要存得進資料庫。

    這條是用血換來的：型態欄位當初只開 varchar(8)，`wire_shelf`（10 字）一存就被
    PostgreSQL 擋下來（StringDataRightTruncationError），但上面那幾條型態測試全是
    純函式、從沒碰過資料庫，所以整套綠燈卻在正式機一 PATCH 就 500。欄位長度是綱要的
    一部分，要用真的寫入來驗。
    """
    from sqlalchemy import select

    from app.models.location import Rack
    from app.services.rack import RACK_KINDS

    for kind in RACK_KINDS:
        db_session.add(Rack(name=f"k-{kind}", u_height=6, kind=kind))
    await db_session.flush()
    got = (await db_session.execute(select(Rack.kind))).scalars().all()
    assert set(got) >= set(RACK_KINDS)


def test_svg_size_does_not_leak_between_kinds() -> None:
    """畫完層架再畫標準機櫃，標準機櫃要維持原本的尺寸。

    尺寸原本是暫時覆蓋模組常數再還原的；只要還原漏掉（或兩張圖同時在畫），後面那張
    就會用到前面那張的寬度 —— 不會報錯，只是圖默默畫錯。
    """
    from app.services.rack_svg import build_rack_svg

    dev = [dict(name="a", type="server", u_position=1, u_size=1,
                rack_slot=0, rack_slot_span=60)]
    before = build_rack_svg("R", 4, dev)
    build_rack_svg("S", 4, dev, kind="wire_shelf", width_mm=1200, row_height_mm=400)
    assert build_rack_svg("R", 4, dev) == before


def test_svg_centres_full_width_labels_like_the_web_diagram() -> None:
    """整列寬的名稱要置中：網頁立面圖是置中的，兩邊不一致看起來像壞掉。"""
    from app.services.rack_svg import build_rack_svg

    svg = build_rack_svg("R", 2, [dict(name="pp-a01", type="patch panel", u_position=1,
                                       u_size=1, rack_slot=0, rack_slot_span=60)])
    # 只找文字元素，別抓到同一段裡的 <title>（滑鼠提示也放同一個名字）
    line = next(ln for ln in svg.split("<text") if ">pp-a01</text>" in ln)
    assert 'text-anchor="middle"' in line


def test_frontend_rack_defaults_match_the_backend() -> None:
    """前端的預設尺寸表必須跟後端一致。

    那份表只是輸入框的提示字（「不填會變成多少」），但提示與實際存進去的值不一樣
    就是在騙人，而且兩邊各改各的不會有任何測試會紅。前端顯示整數 mm，所以比對時
    四捨五入。
    """
    import re
    from pathlib import Path

    from app.services.rack import RACK_KINDS, rack_defaults

    ts = Path(__file__).resolve().parents[2] / "frontend" / "src" / "utils" / "rackSlots.ts"
    src = ts.read_text(encoding="utf-8")
    block = re.search(r"RACK_DEFAULTS[^=]*=\s*\{(.*?)\n\};", src, re.S)
    assert block, "rackSlots.ts 找不到 RACK_DEFAULTS"
    found = {m[0]: (int(m[1]), int(m[2])) for m in re.findall(
        r"(\w+):\s*\{\s*width:\s*(\d+),\s*row:\s*(\d+)\s*\}", block.group(1))}
    assert set(found) == set(RACK_KINDS), f"型態對不上：{sorted(found)}"
    # 層板厚度也是一份兩地的常數，一起比對
    board = dict(re.findall(r"(\w+):\s*(\d+)", re.search(
        r"RACK_BOARD_MM[^=]*=\s*\{(.*?)\};", src, re.S).group(1)))
    from app.services.rack import board_default_mm
    for kind in RACK_KINDS:
        assert int(board[kind]) == round(board_default_mm(kind)), \
            f"{kind} 層板厚度：前端 {board[kind]} vs 後端 {board_default_mm(kind)}"
    for kind, (w, r) in ((k, rack_defaults(k)) for k in RACK_KINDS):
        fw, fr = found[kind]
        assert fw == round(w) and fr == round(r), f"{kind}: 前端 {fw}/{fr} vs 後端 {w}/{r}"


def test_row_height_budget_never_shrinks_an_existing_rack() -> None:
    """列高上限改成「整張圖的高度預算」，但只能放寬、不能收緊。

    一個 5 層、每層 30 公分的層架卡在 76px 會被畫成矮胖樣，跟實物差很遠；但收緊上限
    會讓既有的高機櫃突然變小。所以預算只用來抬高上限，任何既有機櫃的列高都不變。
    """
    from app.services.rack import rack_render_size

    for rows in (1, 6, 12, 16, 20, 42, 99):
        no_rows = rack_render_size("rack", None, None)[1]
        with_rows = rack_render_size("rack", None, None, rows)[1]
        assert with_rows == no_rows == 28.0, rows


def test_tall_shelf_levels_are_drawn_to_scale() -> None:
    """90×150 公分的五層架：高寬比要接近實物，不能被列高上限壓成矮胖的。"""
    from app.services.rack import rack_render_size

    w, r = rack_render_size("wire_shelf", 900, 300, 5)
    ratio = (r * 5) / w
    assert abs(ratio - 1500 / 900) < 0.2, f"比例 {ratio:.2f} 差太多"


def test_many_level_shelf_stays_within_the_height_budget() -> None:
    """層數多的時候上限要收回來，整張圖不能無限長。"""
    from app.services.rack import rack_render_size

    _, r = rack_render_size("shelf", 900, 400, 20)
    assert r * 20 <= 1600.0


def test_wood_shelf_defaults_match_the_ikea_ivar_parts() -> None:
    """木質層架的預設＝使用者實際在用的那組 IKEA IVAR：層板 42×30、側架高 179 公分。

    179 公分的側架官方要求至少 4 層，實務上多半放 6 層 → 每層約 300mm。
    """
    from app.services.rack import rack_defaults, uses_rack_units

    w, r = rack_defaults("wood_shelf")
    assert (w, r) == (420.0, 300.0)
    assert not uses_rack_units("wood_shelf")     # 以「層」計，不是 U


def test_wood_shelf_draws_pine_frames_pegs_and_a_cross_brace() -> None:
    """IVAR 的三個識別特徵都要畫出來：松木側架、整排調整孔、背面的 X 支撐桿。

    側架是一片**平板**不是圓管 —— 不能有圓角（rx），孔是圓孔不是一圈圈的橫紋。
    """
    from app.services.rack_svg import build_rack_svg

    svg = build_rack_svg("IVAR-01", 6, [], kind="wood_shelf", width_mm=420, row_height_mm=298)
    assert "url(#pine)" in svg and "url(#pineboard)" in svg
    assert "url(#pegs)" in svg
    assert "rx=" not in svg, "方柱不該有圓角"
    assert svg.count('stroke="#aeb4ba"') == 2, "OBSERVATÖR 是兩根交叉成 X（官方商品圖）"


def test_brace_span_follows_the_fixed_100cm_bar() -> None:
    """支撐桿是固定 100 公分的鋼條，所以跨幾層由寬度決定，不是固定值。

    對得起 IKEA 的數字：83 公分寬的配置垂直跨距約 21 吋（53 公分）、
    42 公分寬的約 35.5 吋（90 公分）。
    """
    from app.services.rack import brace_levels, brace_span_mm

    assert abs(brace_span_mm(420) - 908) < 5      # ≈ 35.5 吋
    assert abs(brace_span_mm(830) - 558) < 5      # ≈ 21 吋
    # 窄的跨 3 層、寬的只跨 1 層（每層 30 公分）
    assert brace_levels("wood_shelf", 420, 300, 6) == 3
    assert brace_levels("wood_shelf", 830, 300, 6) == 1
    # 跨距要無條件捨去：跨過頭的話那根鋼條實物裝不上去
    assert brace_levels("wood_shelf", 420, 350, 6) == 2      # 908/350 = 2.59 → 2，不是 3
    # 寬過 100 公分就跨不了，不能硬畫
    assert brace_levels("wood_shelf", 1200, 300, 6) == 0
    # 其他型態不畫
    assert brace_levels("wire_shelf", 420, 300, 6) == 0


def test_shelf_kinds_draw_a_top_board() -> None:
    """層架最上面幾乎一定有一片板當頂 —— 只畫每層下緣的話會少一片，看起來像少一層。

    層板是**橫跨整個寬度**的那些方塊；立柱、溝槽、套環用同一組漸層但寬度不同，
    所以用寬度來認，不要用厚度（厚度現在是機櫃設定，會變）。
    """
    import re

    from app.services.rack import level_render_px
    from app.services.rack_svg import build_rack_svg

    for kind in ("shelf", "wire_shelf", "wood_shelf"):
        col_w, _ = level_render_px(kind, None, None, None, 5)
        svg = build_rack_svg("s", 5, [], kind=kind)
        wide = [w for w in re.findall(r'<rect x="[\d.]+" y="[-\d.]+" width="([\d.]+)"', svg)
                if abs(float(w) - col_w) < 0.01]
        assert len(wide) == 6, f"{kind} 應有 5 層 + 1 片頂板，實際 {len(wide)}"



def test_level_heights_fill_in_and_tolerate_bad_data() -> None:
    """逐層高度：沒給、給不夠、給了壞值，都要退回整台的層高，不能畫壞。"""
    from app.services.rack import level_heights_mm

    assert level_heights_mm("wood_shelf", 300, None, 4) == [300] * 4
    assert level_heights_mm("wood_shelf", 300, [500], 4) == [500, 300, 300, 300]
    assert level_heights_mm("wood_shelf", 300, [500, 0, -1, "x"], 4) == [500, 300, 300, 300]
    # 給多了就截掉，不會多畫一層
    assert level_heights_mm("wood_shelf", 300, [1, 2, 3, 4, 5, 6], 2) == [1.0, 2.0]


def test_per_level_heights_change_the_drawing_and_the_brace() -> None:
    """各層高度不同時，圖要跟著變高變矮，支撐桿跨距也要重算。"""
    from app.services.rack import brace_levels, level_render_px
    from app.services.rack_svg import build_rack_svg

    _, even = level_render_px("wood_shelf", 420, 300, None, 6)
    _, vary = level_render_px("wood_shelf", 420, 300, [450, 380, 300, 250, 220, 180], 6)
    assert len(set(even)) == 1, "均一時每層一樣高"
    assert vary[0] > vary[-1], "第 1 層設得比第 6 層高，畫出來就要比較高"

    # 支撐桿逐層累加：450+380=830 ≤ 908，再加 300 會超過 → 只跨 2 層
    assert brace_levels("wood_shelf", 420, 300, 6, [450, 380, 300, 250, 220, 180]) == 2
    assert brace_levels("wood_shelf", 420, 300, 6) == 3

    # 整張圖的高度也要跟著變（不是每層都用同一個值乘層數）
    a = build_rack_svg("s", 6, [], kind="wood_shelf", width_mm=420, row_height_mm=300)
    b = build_rack_svg("s", 6, [], kind="wood_shelf", width_mm=420, row_height_mm=300,
                       level_heights=[450, 380, 300, 250, 220, 180])
    assert a.split('height="')[1] != b.split('height="')[1]


def test_multi_level_device_height_sums_the_levels_it_spans() -> None:
    """跨多層的裝置高度＝它跨過那幾層的高度**總和**，不是層數 × 某一個列高。"""
    import re

    from app.services.rack import level_render_px
    from app.services.rack_svg import build_rack_svg

    heights = [400, 200, 200, 200]
    dev = [dict(name="tall", type="server", u_position=1, u_size=2,
                rack_slot=0, rack_slot_span=60)]
    svg = build_rack_svg("s", 4, dev, kind="wood_shelf", width_mm=420,
                         row_height_mm=300, level_heights=heights)
    _, px = level_render_px("wood_shelf", 420, 300, heights, 4)
    want = px[0] + px[1] - 2          # 方塊上下各內縮 1px（畫面上的間隙）
    got = [float(m) for m in re.findall(
        r'<rect x="[\d.]+" y="[\d.]+" width="[\d.]+" height="([\d.]+)" fill="rgba', svg)]
    assert any(abs(g - want) < 0.01 for g in got), f"找不到高度 {want}，只有 {got}"


def test_ivar_peg_holes_use_the_real_spec() -> None:
    """IVAR 側架的調整孔：孔距（中心至中心）32mm、孔徑 7mm，照圖面比例尺換算。

    前端的 CSS 是另外寫一份（radial-gradient），兩邊對不起來就會一邊密一邊疏，
    所以這裡連前端那份也一起比對。
    """
    import re
    from pathlib import Path

    from app.services.rack_svg import PEG_DIA_MM, PEG_PITCH_MM, _PEG_PITCH, _PEG_R

    assert (PEG_PITCH_MM, PEG_DIA_MM) == (32.0, 7.0)
    assert abs(_PEG_PITCH - 20.16) < 0.05
    assert abs(_PEG_R * 2 - 4.41) < 0.05

    vue = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
           / "RackDiagram.vue").read_text(encoding="utf-8")
    m = re.search(r"transparent [\d.]+px\) 0 0 / 14px ([\d.]+)px", vue)
    assert m, "RackDiagram.vue 找不到調整孔的 background-size"
    assert abs(float(m.group(1)) - _PEG_PITCH) < 0.05, "前端孔距與後端對不起來"


def test_posts_stop_at_the_top_board() -> None:
    """立柱只到最上面那片層板為止 —— 頂板上面是開放的，柱子不會再往上長。"""
    import re

    from app.services.rack_svg import build_rack_svg

    svg = build_rack_svg("s", 4, [], kind="wood_shelf", width_mm=420, row_height_mm=300)
    ys = [float(y) for y, w in re.findall(
        r'<rect x="[-\d.]+" y="([-\d.]+)" width="([\d.]+)"', svg) if abs(float(w) - 14.0) < 0.01]
    boards = [float(y) for y, w in re.findall(
        r'<rect x="[-\d.]+" y="([-\d.]+)" width="([\d.]+)"', svg) if float(w) > 100]
    assert ys and boards
    # 立柱的上緣要低於畫面最上緣（那裡是開放的頂端），且不高於最上面那片板太多
    assert min(ys) > min(boards) - 12, f"立柱 {min(ys)} 畫到最上面那片板 {min(boards)} 之上了"
