"""防火牆規則快照：系統自動規則的異動另外歸類（2026-10-22）

一台 OPNsense 升級韌體後，規則 API 開始連系統自動產生的規則（anti-lockout、預設拒絕、
IPv6 必要規則、CrowdSec／sshlockout 封鎖表…，raw 帶 is_automatic）一起回傳，被記成「新增 28 條」並發出通知。
它們升級前就在作用，不是管理員新增的。程式改為把這種規則放在 diff["auto"]、不通知（services/fw_review.py）；
這裡把已存快照裡同一種規則（OPNsense 系統規則的識別是 32 位十六進位，使用者規則是帶連字號的 UUID）
搬到 diff["auto"]，規則異動頁才不會再把它們當成新增。只動 diff，規則內容與確認狀態不變。

Revision ID: 0226_fw_snapshot_auto_rules
Revises: 0225_anomaly_obs_baseline
"""
from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op

revision: str = "0226_fw_snapshot_auto_rules"
down_revision: str | None = "0225_anomaly_obs_baseline"
branch_labels: str | None = None
depends_on: str | None = None

_AUTO_KEY = re.compile(r"[0-9a-f]{32}")


def _split(diff: dict) -> dict | None:
    auto = {k: list((diff.get("auto") or {}).get(k) or []) for k in ("added", "removed", "changed")}
    moved = False
    out = dict(diff)
    for k in ("added", "removed", "changed"):
        keep = []
        for item in diff.get(k) or []:
            if isinstance(item, dict) and _AUTO_KEY.fullmatch(str(item.get("key") or "")):
                auto[k].append(item)
                moved = True
            else:
                keep.append(item)
        out[k] = keep
    if not moved:
        return None
    out["auto"] = auto
    return out


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, diff FROM fw_rule_snapshots WHERE source_type = 'opnsense' AND diff IS NOT NULL")).all()
    for rid, diff in rows:
        if isinstance(diff, str):
            diff = json.loads(diff)
        if not isinstance(diff, dict):
            continue
        new = _split(diff)
        if new is not None:
            conn.execute(sa.text("UPDATE fw_rule_snapshots SET diff = CAST(:d AS jsonb) WHERE id = :id"),
                         {"d": json.dumps(new, ensure_ascii=False), "id": rid})


def downgrade() -> None:
    # 搬回去會讓系統規則再被當成新增；歸類是單向的資料修正，不還原
    pass
