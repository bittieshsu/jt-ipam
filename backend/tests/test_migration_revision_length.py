"""alembic 版本號不可超過 32 字（alembic_version.version_num 是 varchar(32)）。

2026-10-14 差點出事：`0212_purge_format_only_mac_changes`（34 字）在測試庫升級到最後一步才失敗 ——
migration 內容已經跑完、寫版本號時才爆，升級就卡在半途。新增 migration 時這支先擋下來。
"""
from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def test_every_revision_id_fits_alembic_version_column() -> None:
    found = {}
    for path in VERSIONS.glob("*.py"):
        m = re.search(r'^revision(?::\s*str)?\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
        if m:
            found[path.name] = m.group(1)
    assert len(found) > 200, "一個 migration 都沒讀到（路徑或寫法改了？）"
    too_long = {name: rev for name, rev in found.items() if len(rev) > 32}
    assert not too_long, f"版本號超過 32 字：{too_long}"
