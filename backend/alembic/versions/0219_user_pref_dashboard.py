"""user_preferences.dashboard：儀表板的個人設定（JSONB）

第一個用途：「近 14 日 稽核 / IP 異動 趨勢」可以排除子網路、DHCP 範圍內的位址、只計入部分異動類型
（使用者 2026-10-18）。結構 {"trend": {"exclude_subnet_ids": [...], "exclude_dhcp": bool, "event_groups": [...]}}；
空值＝照舊全部計入。

Revision ID: 0219_user_pref_dashboard
Revises: 0218_ip_state_lifecycle
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0219_user_pref_dashboard"
down_revision: str | None = "0218_ip_state_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_preferences", sa.Column("dashboard", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_preferences", "dashboard")
