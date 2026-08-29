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


def _part_geom(dev: dict[str, Any], rack_left: float) -> tuple[float, float, float, bool]:
    """半 U 裝置佔左半或右半；回 (x, width, center_x, is_half)。"""
    side = dev.get("rack_side") or "full"
    if side == "left":
        return rack_left + 2, COL_W / 2 - 3, rack_left + COL_W / 4, True
    if side == "right":
        return rack_left + COL_W / 2 + 1, COL_W / 2 - 3, rack_left + COL_W * 3 / 4, True
    return rack_left + 2, COL_W - 4, rack_left + COL_W / 2, False


def build_rack_svg(name: str, u_height: int, devices: list[dict[str, Any]]) -> str:
    """單一機櫃的 SVG。`devices` 每筆要有 name / type / u_position / u_size /
    rack_side / rack_face。"""
    u = max(int(u_height or 0), 1)
    rack_left = PAD + GUTTER
    top = HEADER_H + PAD
    width = PAD * 2 + GUTTER + COL_W
    height = HEADER_H + PAD * 2 + u * ROW_H

    p: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{rack_left}" y="{PAD + 16}" font-size="14" font-weight="bold">'
        f"{_esc(name)} ({u}U)</text>",
        f'<rect x="{rack_left}" y="{top}" width="{COL_W}" height="{u * ROW_H}" '
        f'fill="#f5f5f5" stroke="#888" stroke-width="1.5"/>',
    ]
    for i in range(u):
        y = top + i * ROW_H
        p.append(
            f'<text x="{rack_left - 4}" y="{y + ROW_H / 2 + 4}" font-size="10" '
            f'text-anchor="end" fill="#666">{u - i}</text>'
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
        y_top = top + (u - u_top) * ROW_H
        hgt = int(size) * ROW_H
        x, w, cx, half = _part_geom(dev, rack_left)
        color = _COLORS.get(str(dev.get("type") or ""), _DEFAULT_COLOR)
        p.append(
            f'<rect x="{x}" y="{y_top + 1}" width="{w}" height="{hgt - 2}" '
            f'fill="{color}" stroke="rgba(0,0,0,0.3)"/>'
        )
        tx = cx if half else rack_left + 10
        anchor = "middle" if half else "start"
        p.append(
            f'<text x="{tx}" y="{y_top + hgt / 2 + 4}" text-anchor="{anchor}" '
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
