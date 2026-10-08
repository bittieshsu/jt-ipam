"""checkpoint_gaia_scripts_on：閘道的 ARP 表與 DHCP 租約預設就讀

使用者 2026-10-08：「抓 dhcp 跟 arp 直接實作，抓不到沒關係，給客戶測」。帳號不能執行指令時同步只記成略過
（不算失敗），所以預設打開沒有副作用；1.0.3 建好的閘道連線一併打開（那時是預設關，沒有人特意選過）。

Revision ID: 0196_checkpoint_gaia_scripts_on
Revises: 0195_checkpoint_gaia
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0196_checkpoint_gaia_scripts_on"
down_revision: str | None = "0195_checkpoint_gaia"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("checkpoint_gaia_targets", "allow_scripts", server_default=sa.true())
    op.execute("UPDATE checkpoint_gaia_targets SET allow_scripts = true WHERE allow_scripts = false")


def downgrade() -> None:
    op.alter_column("checkpoint_gaia_targets", "allow_scripts", server_default=sa.false())
