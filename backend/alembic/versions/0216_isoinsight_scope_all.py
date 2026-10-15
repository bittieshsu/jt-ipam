"""isoinsight_sources.scope_all：允許同步的子網路＝所選單位底下的全部（使用者 2026-10-15）

「預設可以默認全部子網路嗎」：新增來源時預設全部，之後新建的子網路自動納入；仍不跨單位（規格：不可跨租戶配對）。
既有來源維持原本選的清單（預設 false）。

Revision ID: 0216_isoinsight_scope_all
Revises: 0215_ip_liveness_from_device
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0216_isoinsight_scope_all"
down_revision: str | None = "0215_ip_liveness_from_device"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("isoinsight_sources", sa.Column("scope_all", sa.Boolean(), nullable=False,
                                                  server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("isoinsight_sources", "scope_all")
