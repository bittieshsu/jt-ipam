"""兩份 CHANGELOG 的最新版本要一致，而且不可落後目前的版本號（v0.6.56 發版時漏了繁中版，2026-10-01 補上）。

英文版寫了、繁中版停在上一版 —— 沒有任何檢查會發現，公開的變更記錄就少一版。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.version import __version__

ROOT = Path(__file__).resolve().parents[2]
HEADER = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def _versions(name: str) -> list[str]:
    return HEADER.findall((ROOT / name).read_text(encoding="utf-8"))


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def test_both_changelogs_have_the_same_latest_version() -> None:
    en, zh = _versions("CHANGELOG.md"), _versions("CHANGELOG_zh-TW.md")
    assert en
    assert zh
    assert en[0] == zh[0], f"CHANGELOG.md 最新是 {en[0]}、CHANGELOG_zh-TW.md 最新是 {zh[0]}"


def test_changelog_is_not_behind_the_version() -> None:
    latest = _versions("CHANGELOG.md")[0]
    assert _key(latest) >= _key(__version__), f"版本號已是 {__version__}，CHANGELOG 最新卻是 {latest}"


def test_no_empty_unreleased_section() -> None:
    """發版時把 [Unreleased] 改名成版本號，不留空的標題（使用者 2026-10-09：留著讓人以為還沒發布）。

    有新改動時再加回 `## [Unreleased]` 並寫在它底下；只要出現，底下就一定要有內容。
    """
    for name in ("CHANGELOG.md", "CHANGELOG_zh-TW.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        m = re.search(r"^## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
        if m:
            assert m.group(1).strip(), f"{name}：[Unreleased] 底下是空的，發版時應該直接改名成版本號"


def test_readme_titles_show_the_current_version() -> None:
    """README 的標題寫死版本號，升版時從沒跟著改：1.0.6 發布時 GitHub 首頁還寫 v0.6.51、日文版 v0.6.35
    （2026-10-10 使用者在手機上看到）。三份 README 的標題都要等於目前的版本。"""
    import re
    root = Path(__file__).resolve().parents[2]
    for name in ("README.md", "README_zh-TW.md", "README_ja.md"):
        first = (root / name).read_text(encoding="utf-8").splitlines()[0]
        m = re.fullmatch(r"# jt-ipam v(\d+\.\d+\.\d+)", first.strip())
        assert m, f"{name} 第一行不是「# jt-ipam vX.Y.Z」：{first!r}"
        assert m.group(1) == __version__, f"{name} 標題寫 v{m.group(1)}，目前版本是 {__version__}"
