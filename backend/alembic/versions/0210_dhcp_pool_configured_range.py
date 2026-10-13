"""dhcp_pool_ranges.pool_start／pool_end：扣掉排除區間之前設定的範圍（客戶 2026-10-12）

0207 起範圍寫入前先扣掉排除區間，一個 scope 會被切成好幾段各存一筆；使用率與「集區快用完」卻把每一段當成
一個集區（Windows 從頭依序發放 → 第一段租滿就報 100%）。記下原始範圍，讀取端以它為單位合併。
既有資料不回推：各來源下一輪同步（幾分鐘內）就會寫入；在那之前讀取端把每一段當成自己的範圍（與升級前相同）。

Revision ID: 0210_dhcp_pool_configured_range
Revises: 0209_ip_device_since
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0210_dhcp_pool_configured_range"
down_revision: str | None = "0209_ip_device_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dhcp_pool_ranges", sa.Column("pool_start", sa.String(64)))
    op.add_column("dhcp_pool_ranges", sa.Column("pool_end", sa.String(64)))


def downgrade() -> None:
    op.drop_column("dhcp_pool_ranges", "pool_end")
    op.drop_column("dhcp_pool_ranges", "pool_start")
