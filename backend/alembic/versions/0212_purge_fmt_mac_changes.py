"""清掉「MAC 只是寫法不同」的假異動記錄（使用者 2026-10-14）

同一個 MAC 一邊有冒號一邊沒有（`aa:bb:cc:dd:ee:ff` → `aabbccddeeff`）被記成 MAC 變動：正式站台
2026-09-17～10-05 累積 2,168 筆，出現在 IP 時間軸與 MAC 歷程，也讓「裝置關聯可能過期」誤報。
寫入端（services/ip_history.log_change）從這一版起不再記這種；這裡把既有的清掉。
只刪 field='mac'、新舊值都有、去掉分隔符號後轉小寫完全相同的列。不可逆（被刪的本來就不是異動）。

Revision ID: 0212_purge_fmt_mac_changes
Revises: 0211_librenms_vlan_domain
"""
from __future__ import annotations

from alembic import op

revision: str = "0212_purge_fmt_mac_changes"
down_revision: str | None = "0211_librenms_vlan_domain"
branch_labels = None
depends_on = None

PURGE_SQL = (
    "DELETE FROM ip_change_log WHERE field = 'mac' AND old_value IS NOT NULL AND new_value IS NOT NULL "
    "AND lower(regexp_replace(old_value, '[^0-9A-Fa-f]', '', 'g')) "
    "= lower(regexp_replace(new_value, '[^0-9A-Fa-f]', '', 'g'))"
)


def upgrade() -> None:
    op.execute(PURGE_SQL)


def downgrade() -> None:
    pass   # 被刪的是假記錄，不還原
