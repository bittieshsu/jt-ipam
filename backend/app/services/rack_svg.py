"""機櫃示意圖的伺服器端 SVG。

畫面上的機櫃圖是前端畫的（`utils/rackGraphicsExport.ts`），但要讓**別的系統**嵌入
（LibreNMS dashboard 的 widget 之類）就必須有一個純網址能拿到圖 —— 對方不會跑我們的
前端。所以這裡把同一套幾何與配色搬到後端。

刻意輸出 **SVG 而不是 PNG**：PNG 需要額外的繪圖套件（cairo 之類），為了一張由矩形和
文字組成的圖引進一個系統相依不划算；SVG 在 `<img>` 裡一樣能顯示，而且縮放不糊。

⚠️ 這張圖會被貼到別人的頁面上，所有文字都必須跳脫（裝置名稱是使用者輸入）。
"""

from __future__ import annotations

from typing import Any

from app.services.rack import (
    RACK_REF_ROW_MM,
    RACK_REF_ROW_PX,
    RACK_SLOTS,
    board_default_mm,
    brace_levels,
    has_open_top,
    level_render_px,
    uses_rack_units,
)

#: 與前端 `GEO` 相同的幾何，改這裡要同步改 `rackGraphicsExport.ts`，否則兩邊會長得不一樣
ROW_H = 24
COL_W = 260
GUTTER = 32
PAD = 12
HEADER_H = 30

#: 與前端 `rackTypeColor()` 相同的配色
_COLORS: dict[str, str] = {
    "router": "rgba(99, 102, 241, 0.85)",
    "switch": "rgba(34, 197, 94, 0.85)",
    "firewall": "rgba(239, 68, 68, 0.85)",
    "ap": "rgba(59, 130, 246, 0.85)",
    "server": "rgba(107, 114, 128, 0.85)",
    "storage": "rgba(245, 158, 11, 0.85)",
    "ipmi": "rgba(236, 72, 153, 0.6)",
    "patch_panel": "rgba(20, 184, 166, 0.75)",
    "pdu": "rgba(217, 119, 6, 0.8)",
    "ups": "rgba(202, 138, 4, 0.85)",
}
_DEFAULT_COLOR = "rgba(107, 114, 128, 0.6)"


def _esc(v: Any) -> str:
    """SVG 文字跳脫。裝置名稱是使用者輸入，少了這個就是注入點。"""
    return (
        str(v if v is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# 窄到這個寬度以下就不畫名稱（擠不下，畫了只是一團糊）。改用 <title> 讓瀏覽器顯示
# 原生 tooltip —— 嵌入用的 SVG 沒有我們自己的 hover 層，這是唯一能把名稱留住的辦法。
_MIN_LABEL_W = 46.0


def _part_geom(dev: dict[str, Any], rack_left: float,
               col_w: float) -> tuple[float, float, float]:
    """依橫向格位算出 (x, width, center_x)。與前端 RackDiagram 的 partGeom 同一套幾何。"""
    slot = int(dev.get("rack_slot") or 0)
    span = int(dev.get("rack_slot_span") or RACK_SLOTS)
    cell = col_w / RACK_SLOTS
    x = rack_left + 2 + slot * cell
    w = span * cell - 4
    return x, max(w, 2.0), x + max(w, 2.0) / 2


def build_rack_svg(name: str, u_height: int, devices: list[dict[str, Any]],
                   *, kind: str | None = None, width_mm: int | None = None,
                   row_height_mm: int | None = None,
                   level_heights: list[int] | None = None,
                   board_mm: int | None = None) -> str:
    """單一機櫃的 SVG。`devices` 每筆要有 name / type / u_position / u_size /
    rack_slot / rack_slot_span / rack_face。

    kind/width_mm/row_height_mm/level_heights 給非標準的層架用（issue #30）；
    不給就是標準 19" 機櫃，
    尺寸與改版前完全相同。
    """
    col_w, row_px = level_render_px(kind, width_mm, row_height_mm, level_heights, u_height)
    braces = brace_levels(kind, width_mm, row_height_mm, u_height, level_heights)
    # 層板厚度也要換成 px，否則層板只是畫上去的裝飾、不占高度，總高會比實物矮一截
    board = board_default_mm(kind) if board_mm is None else float(board_mm)
    board_px = RACK_REF_ROW_PX * (board / RACK_REF_ROW_MM) if board > 0 else 0.0
    return _build(name, u_height, devices, kind or "rack", col_w, row_px, braces,
                  max(board_px, 2.0) if board > 0 else 0.0, has_open_top(kind))


# 鍍鉻層架的圓管立柱漸層（與前端 .rack-frame.is-wire 的 CSS 同一組色，兩邊要看起來一樣）
_CHROME = (
    '<linearGradient id="chrome" x1="0" y1="0" x2="1" y2="0">'
    '<stop offset="0%" stop-color="#6f7479"/><stop offset="18%" stop-color="#9aa0a6"/>'
    '<stop offset="38%" stop-color="#eef1f3"/><stop offset="48%" stop-color="#ffffff"/>'
    '<stop offset="62%" stop-color="#d3d8dc"/><stop offset="82%" stop-color="#9aa0a6"/>'
    '<stop offset="100%" stop-color="#63686d"/></linearGradient>'
)

# 立柱整根的細溝槽環：鍍鉻層架最好認的特徵，沒有它只是兩根灰長條（前端用
# repeating-linear-gradient 做同一件事）。pattern 的高度＝溝槽間距。
_GROOVES = (
    '<pattern id="grooves" width="1" height="5" patternUnits="userSpaceOnUse">'
    '<rect x="0" y="0" width="100%" height="1" fill="rgba(70,76,82,0.20)"/></pattern>'
)


# 松木側架的木紋漸層與孔位。IVAR 的側架是一片有整排孔位的松木框 —— 孔位那一排
# 是它最好認的特徵，跟鍍鉻層架的圓管立柱完全是兩回事（前端用同一組色）。
# 側架是一片**平的**松木板，不是圓管：整片幾乎同一個木色，只有右緣一條窄窄的暗邊
# 當厚度，沒有圓柱那種由暗到亮再到暗的漸層。
_PINE = (
    '<linearGradient id="pine" x1="0" y1="0" x2="1" y2="0">'
    '<stop offset="0%" stop-color="#dcb98d"/><stop offset="80%" stop-color="#d4ae7f"/>'
    '<stop offset="100%" stop-color="#b38a58"/></linearGradient>'
)
_PINE_BOARD = (
    '<linearGradient id="pineboard" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0%" stop-color="#e9c99d"/><stop offset="55%" stop-color="#d2a56f"/>'
    '<stop offset="100%" stop-color="#a5763f"/></linearGradient>'
)
# 側架上的調整孔：整排貫穿，是 IVAR 一眼就認得出來的地方。
# 實物規格：孔距（中心至中心）32mm、孔徑 7mm —— 照圖面的比例尺換算成 px，
# 不要用隨手挑的像素值，否則放大看就和層板的比例對不起來。
# pattern 的寬度要等於側架寬度，否則圓會被 1px 的圖磚裁掉（第一版就是這樣什麼都沒畫出來）。
_SIDE_W = 14.0
PEG_PITCH_MM = 32.0
PEG_DIA_MM = 7.0
_PEG_PITCH = RACK_REF_ROW_PX * (PEG_PITCH_MM / RACK_REF_ROW_MM)
_PEG_R = RACK_REF_ROW_PX * (PEG_DIA_MM / RACK_REF_ROW_MM) / 2
_PEGS = (
    f'<pattern id="pegs" width="{_SIDE_W}" height="{_PEG_PITCH:.3f}" '
    f'patternUnits="userSpaceOnUse">'
    f'<circle cx="{_SIDE_W / 2}" cy="{_PEG_PITCH / 2:.3f}" r="{_PEG_R:.3f}" '
    f'fill="rgba(74,48,24,0.5)"/>'
    f'<circle cx="{_SIDE_W / 2}" cy="{_PEG_PITCH / 2 - 0.5:.3f}" r="{_PEG_R:.3f}" '
    f'fill="rgba(255,240,215,0.30)"/></pattern>'
)
# 木紋：幾道很淡的直紋，讓側架不會像一塊純色
_GRAIN = (
    '<pattern id="grain" width="7" height="1" patternUnits="userSpaceOnUse">'
    '<rect x="0" y="0" width="1" height="100%" fill="rgba(120,80,40,0.10)"/></pattern>'
)


def _brace(left: float, u: int, levels: int,
           COL_W: float, bounds: list[float]) -> list[str]:
    """木質層架背面的 OBSERVATÖR 支撐桿：**兩根交叉成 X**（官方商品圖就是 X）。

    每根是固定 100 公分的鋼條，所以 X 的垂直跨距由層架寬度決定（見 rack.py 的
    brace_span_mm）—— 寬的跨得矮、窄的跨得高。裝在最底下那一段。
    """
    if levels <= 0:
        return []
    y_bot = bounds[u] - 2
    y_top = bounds[u - levels] + 2
    x0, x1 = left + 4, left + COL_W - 4
    return [
        f'<line x1="{a}" y1="{y_bot}" x2="{b}" y2="{y_top}" '
        f'stroke="#aeb4ba" stroke-width="2.5" stroke-linecap="round" opacity="0.9"/>'
        for a, b in ((x0, x1), (x1, x0))
    ]


def _frame(kind: str, left: float, u: int, COL_W: float,
           bounds: list[float], brace_levels: int = 0,
           board_px: float = 0.0, open_top: bool = False) -> list[str]:
    """依型態畫外框：標準／工業是箱體，層架類不封邊，鍍鉻層架另外畫兩根圓管立柱。

    `bounds` 是每一層的邊界 y（共 u+1 個，由上往下）—— 層架的層高可以一層一層不同，
    所以位置一律查這張表，不能用「第幾層 × 固定列高」去算。
    """
    top, h = bounds[0], bounds[u] - bounds[0]
    bd = max(board_px, 2.0)      # 層板畫多厚（至少 2px，否則細到看不見）
    # 層板的位置。開放頂端時最上面那一列的**上面**沒有板（那裡就是開放的），
    # 所以跳過第一個邊界 —— 不跳的話 5 層會畫出 7 片板。
    boards = bounds[1:] if open_top else bounds
    # 立柱／側架只到**最上面那片層板**為止，不會再往上長 —— 頂板上面是開放的，
    # 東西就放在那裡。所以開放頂端時立柱從 boards[0] 起算，不是從畫面最上緣。
    p_top = boards[0]
    p_h = bounds[u] - p_top
    if kind == "industrial":
        return [f'<rect x="{left}" y="{top}" width="{COL_W}" height="{h}" '
                f'fill="#eceef1" stroke="#5a5f69" stroke-width="4"/>']
    if kind == "wire_shelf":
        post = 11.0

        def _post(x: float) -> list[str]:
            # 圓管 → 溝槽 → 每層的套環，三層疊上去（順序＝前端那三層背景）
            return [
                f'<rect x="{x}" y="{p_top - 6}" width="{post}" height="{p_h + 12}" '
                f'rx="{post / 2}" fill="url(#chrome)" stroke="rgba(0,0,0,.18)"/>',
                f'<rect x="{x}" y="{p_top - 6}" width="{post}" height="{p_h + 12}" '
                f'rx="{post / 2}" fill="url(#grooves)"/>',
                *[f'<rect x="{x}" y="{b - bd}" width="{post}" height="{bd + 2}" '
                  f'fill="url(#chrome)" stroke="rgba(60,66,72,.5)" stroke-width="0.8"/>'
                  for b in boards],
            ]

        return [
            f"<defs>{_CHROME}{_GROOVES}</defs>",
            # 層板（含最上面的頂板：層架頂端幾乎一定有一片，少畫了就像少一層）
            *[f'<rect x="{left}" y="{b - bd}" width="{COL_W}" height="{bd}" '
              f'fill="url(#chrome)"/>' for b in boards],
            # 兩側圓管立柱（畫在層板之上，看起來像穿過去）
            *_post(left - post / 2),
            *_post(left + COL_W - post / 2),
        ]
    if kind == "wood_shelf":
        side = _SIDE_W         # 松木側架比圓管粗，它是一片框不是一根柱
        def _side(x: float) -> list[str]:
            return [
                # 方柱：沒有圓角（IVAR 的側架是方料，不是圓管）
                f'<rect x="{x}" y="{p_top - 8}" width="{side}" height="{p_h + 16}" '
                f'fill="url(#pine)" stroke="rgba(90,60,30,.55)" stroke-width="1"/>',
                f'<rect x="{x}" y="{p_top - 8}" width="{side}" height="{p_h + 16}" '
                f'fill="url(#grain)"/>',
                f'<rect x="{x}" y="{p_top - 8}" width="{side}" height="{p_h + 16}" '
                f'fill="url(#pegs)"/>',
            ]
        return [
            f"<defs>{_PINE}{_PINE_BOARD}{_PEGS}{_GRAIN}</defs>",
            # 背面的 OBSERVATÖR 支撐桿（X）：跨距由寬度決定（見 rack.py）。
            # 畫在層板與裝置的「後面」—— 實物就在背面，空層才看得到它，順序不能反。
            *_brace(left, u, brace_levels, COL_W, bounds),
            # 層板：實心松木，比鍍鉻的橫桿厚。range 從 0 起＝連最上面的頂板一起畫：
            # 層架最上面幾乎一定有一片板當頂，少畫了就會像少一層。
            *[f'<rect x="{left}" y="{b - bd}" width="{COL_W}" height="{bd}" '
              f'fill="url(#pineboard)" stroke="rgba(90,60,30,.35)" stroke-width="0.6"/>'
              for b in boards],
            # 側架整根畫在層板外側（實物就是層板架在兩片側架之間），不像鍍鉻立柱那樣
            # 跨在角落 —— 跨過去會被裝置方塊蓋掉半邊，孔位就看不見了。
            *_side(left - side),
            *_side(left + COL_W),
        ]
    if kind == "shelf":
        return [f'<rect x="{left}" y="{b - bd}" width="{COL_W}" height="{bd}" '
                f'fill="#8b9096"/>' for b in boards]
    return [f'<rect x="{left}" y="{top}" width="{COL_W}" height="{h}" '
            f'fill="#f5f5f5" stroke="#888" stroke-width="1.5"/>']


def _build(name: str, u_height: int, devices: list[dict[str, Any]],
           kind: str, col_w: float, row_px: list[float], brace_levels: int = 0,
           board_px: float = 0.0, open_top: bool = False) -> str:
    # 尺寸用參數傳，不改模組常數：同一支程序可能同時畫標準機櫃與層架，
    # 暫時覆蓋全域值在任何並行情境下都會畫錯（而且不會報錯，只是尺寸不對）。
    COL_W = col_w
    u = max(int(u_height or 0), 1)
    rack_left = PAD + GUTTER
    top = HEADER_H + PAD
    # 每一層各自的高度（`row_px` 由第 1 層起算），換成由上往下的邊界表。
    # 層架的層高可以一層一層不同，所以任何位置都查這張表，不能乘固定列高。
    px = list(row_px) if row_px else [ROW_H] * u
    px = (px + [px[-1]] * u)[:u]
    # 層架的最上面那片板**上面**也放得了東西 → 多畫一列（第 u+1 層），高度比照最高那層。
    rows_drawn = u + 1 if open_top else u
    if open_top:
        px = [*px, px[-1]]
    bounds: list[float] = [top]
    for i in range(rows_drawn):
        # 由上往下＝從最高的那一列開始；每一列下面還有一片層板要占高度
        bounds.append(bounds[-1] + px[rows_drawn - 1 - i] + board_px)
    width = PAD * 2 + GUTTER + COL_W
    height = HEADER_H + PAD * 2 + (bounds[rows_drawn] - top)

    p: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{rack_left}" y="{PAD + 16}" font-size="14" font-weight="bold">'
        f"{_esc(name)} ({u}{'U' if uses_rack_units(kind) else _esc(' 層')})</text>",
        *_frame(kind, rack_left, rows_drawn, COL_W, bounds, brace_levels,
                board_px, open_top),
    ]
    for i in range(rows_drawn):
        y, y2 = bounds[i], bounds[i + 1]
        lvl = rows_drawn - i
        # 頂板上方那一列沒有層號，標「頂」—— 它不是第 11 層，是第 10 層的板子上面
        label = _esc(" 頂") if (open_top and lvl > u) else str(lvl)
        p.append(
            f'<text x="{rack_left - 4}" y="{(y + y2) / 2 + 4}" font-size="10" '
            f'text-anchor="end" fill="#666">{label}</text>'
        )
        p.append(
            f'<line x1="{rack_left}" y1="{y}" x2="{rack_left + COL_W}" y2="{y}" '
            f'stroke="#dddddd" stroke-width="0.5"/>'
        )

    for dev in devices:
        pos, size = dev.get("u_position"), dev.get("u_size")
        if not pos or not size:
            continue                      # 沒有位置的裝置不畫（畫了也不知道畫在哪）
        u_top = int(pos) + int(size) - 1
        # 裝置從第 pos 層佔到第 pos+size-1 層：上緣是最高那層的上邊界，
        # 高度是它跨過那幾層的高度總和（各層高度可能不同，不能乘 size）。
        y_top = bounds[rows_drawn - u_top]
        hgt = sum(px[int(pos) - 1:int(pos) - 1 + int(size)])
        # 層內的垂直位置：層架一層可以疊放、也可以不放滿。
        # 0 貼著層板 ＝ 這一段的**下緣**，所以由下往上長。
        v0 = int(dev.get("rack_vslot") or 0)
        vspan = int(dev.get("rack_vslot_span") or RACK_SLOTS)
        if v0 or vspan < RACK_SLOTS:
            cell = hgt / RACK_SLOTS
            y_bot = y_top + hgt
            hgt = vspan * cell
            y_top = y_bot - (v0 + vspan) * cell
        x, w, cx = _part_geom(dev, rack_left, COL_W)
        color = _COLORS.get(str(dev.get("type") or ""), _DEFAULT_COLOR)
        p.append(
            f'<rect x="{x}" y="{y_top + 1}" width="{w}" height="{hgt - 2}" '
            f'fill="{color}" stroke="rgba(0,0,0,0.3)"/>'
        )
        # 名稱永遠進 <title>（滑過看得到）；太窄就不畫文字，避免糊成一團
        p.append(f'<title>{_esc(dev.get("name"))}</title>')
        if w >= _MIN_LABEL_W:
            # 一律置中（含整列寬）：網頁版的立面圖就是置中，兩邊不一致看起來像 bug。
            # 靠左是舊的 full / left / right 模型留下來的，那時整列寬是唯一的「非一半」。
            p.append(
                f'<text x="{cx}" y="{y_top + hgt / 2 + 4}" text-anchor="middle" '
                f'font-size="11" font-weight="bold" fill="#ffffff">{_esc(dev.get("name"))}</text>'
            )
        if dev.get("rack_face") == "rear":
            rx = x + w
            p.append(
                f'<path d="M{rx - 14} {y_top + 1} L{rx} {y_top + 1} L{rx} {y_top + 15} Z" '
                f'fill="rgba(0,0,0,0.55)"/>'
            )
            p.append(
                f'<text x="{rx - 2}" y="{y_top + 11}" text-anchor="end" font-size="9" '
                f'font-weight="bold" fill="#ffffff">R</text>'
            )
    p.append("</svg>")
    return "\n".join(p)
