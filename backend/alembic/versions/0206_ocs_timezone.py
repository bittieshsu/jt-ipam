"""ocs_servers.timezone：OCS 的 LASTDATE 是 OCS 伺服器的本地時間（issue #51）

以前固定當成 UTC；時區不是 UTC 的 OCS，最後盤點時間會偏移（UTC+8 超前 8 小時、甚至在未來）。
既有的整合設成 UTC＝維持升級前的結果；新建的整合預設用 jt-ipam 主機的時區（API 端決定）。
同步時換算後落在未來，結果會提醒時區可能設錯。

Revision ID: 0206_ocs_timezone
Revises: 0205_subnet_scan_scope
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0206_ocs_timezone"
down_revision: str | None = "0205_subnet_scan_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ocs_servers", sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"))


def downgrade() -> None:
    op.drop_column("ocs_servers", "timezone")
