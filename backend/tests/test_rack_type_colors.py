"""機櫃立面圖的設備類型配色（10-23 客戶：「server 跟其他最好不要都是很近的灰色」）。

伺服器以前是灰色、「其他」也是灰色只差透明度，深色主題下整櫃灰灰的分不出來。
伺服器改成有色相的鋼藍；「其他」維持中性灰但加斜紋，一看就是未分類／配件。
畫面（rackColors.ts）與對外嵌入 SVG（rack_svg.py）是兩份配色，必須一樣。
"""
from __future__ import annotations

import colorsys
import re
from pathlib import Path

from app.services.rack_svg import _COLORS, _DEFAULT_COLOR, build_rack_svg


def _rgb(c: str) -> tuple[float, float, float]:
    r, g, b = (int(x) for x in re.findall(r"\d+", c)[:3])
    return r / 255, g / 255, b / 255


def test_server_has_a_hue_and_is_not_the_other_grey() -> None:
    h, l, s = colorsys.rgb_to_hls(*_rgb(_COLORS["server"]))
    assert s >= 0.3, "伺服器不能是灰色（飽和度太低就跟「其他」分不出來）"
    assert _rgb(_COLORS["server"]) != _rgb(_DEFAULT_COLOR)


def test_frontend_palette_matches_the_embedded_svg() -> None:
    ts = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "utils" / "rackColors.ts").read_text("utf-8")
    found = dict(re.findall(r'case "(\w+)":\s*\n\s*return "([^"]+)";', ts))
    assert found == _COLORS
    assert f'RACK_OTHER_COLOR = "{_DEFAULT_COLOR}"' in ts


def test_other_and_unknown_types_are_striped_in_the_embedded_svg() -> None:
    devs = [{"name": "kvm-1", "type": "other", "u_position": 1, "u_size": 1, "rack_face": "front"},
            {"name": "srv-1", "type": "server", "u_position": 2, "u_size": 1, "rack_face": "front"}]
    svg = build_rack_svg("R1", 4, devs)
    assert 'id="rd-other"' in svg
    assert 'fill="url(#rd-other)"' in svg
    assert f'fill="{_COLORS["server"]}"' in svg
