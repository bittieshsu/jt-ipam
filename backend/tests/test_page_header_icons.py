"""每個頁面最上面那張卡片的標題都要有圖示（使用者 2026-10-15：「別的卡片標題都有 icon，為何 MAC 歷程沒有」）。

一查還有四頁也沒有（IP 異動記錄、通知、主機名稱來源優先序、IP 申請詳細）：用 `:title="…"` 寫的卡片
標題放不進圖示，`#header` 裡也有人只放了文字。這支測試掃路由上的每個頁面，第一張卡片：
- 不可以用 `title` 屬性（放不進圖示）
- `#header` 裡要有圖示元件
"""
from __future__ import annotations

import re
from pathlib import Path

FE = Path(__file__).resolve().parents[2] / "frontend" / "src"

#: 不是一般頁面（登入頁、主控台全畫面、沒有標題卡片的頁面）
EXEMPT = {
    "Login.vue": "登入頁的卡片標題是品牌標誌",
    "SystemSettings.vue": "頁面標題在卡片外面（ss-title，有圖示）；卡片是設定分組",
}


def _routed_views() -> list[Path]:
    router = (FE / "router" / "index.ts").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r'import\("@/views/([A-Za-z0-9]+\.vue)"\)', router)))
    assert len(names) > 60, "路由解析不到頁面（格式改了？）"
    return [FE / "views" / n for n in names]


def _first_card_problem(src: str) -> str | None:
    tpl = re.search(r"^<template>\n(.*)^</template>", src, re.S | re.M)
    if not tpl:
        return None
    body = tpl.group(1)
    m = re.search(r"<n-card\b((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", body)
    if not m:
        return None                     # 這頁沒有卡片（例如儀表板、全畫面主控台）
    if re.search(r"(?:^|\s)(?::title|title)=", m.group(1)):
        return "第一張卡片用 title 屬性：放不進圖示，改用 <template #header> 加 n-icon"
    rest = body[m.end():].lstrip()
    # 跳過卡片開頭的註解。不用 `\s*(?:<!--.*?-->\s*)*` 這種巢狀量詞：CodeQL #53 判定會大量回溯
    while rest.startswith("<!--"):
        end = rest.find("-->")
        if end < 0:
            return None
        rest = rest[end + 3:].lstrip()
    if not rest.startswith("<template #header>"):
        return None                     # 沒有標題列的卡片（例如純篩選列）
    close = rest.find("</template>")
    header = rest[len("<template #header>"):close if close >= 0 else None]
    if not re.search(r"<n-icon\b|<[A-Z][A-Za-z]*Icon\b|<component :is=|:icon=", header):
        return "第一張卡片的 #header 沒有圖示"
    return None


def test_every_page_header_has_an_icon() -> None:
    bad = []
    for p in _routed_views():
        if p.name in EXEMPT or not p.exists():
            continue
        problem = _first_card_problem(p.read_text(encoding="utf-8"))
        if problem:
            bad.append(f"{p.name}: {problem}")
    assert not bad, "頁面標題缺圖示：\n" + "\n".join(bad)


def test_the_check_catches_a_title_attribute() -> None:
    assert _first_card_problem('<template>\n  <n-card :title="t(\'x\')">\n  </n-card>\n</template>\n')
    assert _first_card_problem("<template>\n  <n-card>\n    <template #header>{{ t('x') }}</template>\n"
                               "  </n-card>\n</template>\n")
    assert _first_card_problem("<template>\n  <n-card>\n    <template #header>\n      <n-icon><XIcon /></n-icon>\n"
                               "    </template>\n  </n-card>\n</template>\n") is None
