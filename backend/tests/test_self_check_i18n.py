"""系統診斷每一項的標題、說明、修法都要三個語系都有。

診斷項的句子由前端依 `doctor.<key>` 組；漏了某個語系，那個語系的使用者看到的是代碼本身，
而且什麼錯都不會報（新增檢查項時最容易漏）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "app" / "services" / "self_check.py"
LOCALES = ("zh-TW", "en-US", "ja-JP")


def test_every_doctor_key_is_translated() -> None:
    keys = set(re.findall(r'"doctor\.([a-z0-9_]+)"', SRC.read_text(encoding="utf-8")))
    assert len(keys) > 20, "一個鍵都找不到 → 掃描本身壞了"
    for loc in LOCALES:
        d = json.loads((ROOT / "frontend" / "src" / "i18n" / f"{loc}.json").read_text(encoding="utf-8"))["doctor"]
        missing = sorted(k for k in keys if k not in d)
        assert not missing, f"{loc} 缺少系統診斷的翻譯：{missing}"
