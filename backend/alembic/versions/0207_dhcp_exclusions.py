"""dhcp_exclusions：DHCP 伺服器設定的排除區間（客戶 2026-10-12）

客戶的 Windows DHCP 把設了固定 IP 的設備逐一排除在發放範圍外，jt-ipam 以前不讀排除區間：
那些位址被當成「在 DHCP 範圍內」。各來源（Windows DHCP、FortiGate、Technitium、Check Point）
下一輪同步寫入；發放範圍同時改成扣掉排除區間後才寫（不需要遷移資料，下一輪同步就換掉）。

Revision ID: 0207_dhcp_exclusions
Revises: 0206_ocs_timezone
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0207_dhcp_exclusions"
down_revision: str | None = "0206_ocs_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dhcp_exclusions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_name", sa.String(128)),
        sa.Column("subnet_cidr", sa.String(64)),
        sa.Column("start_ip", sa.String(64), nullable=False),
        sa.Column("end_ip", sa.String(64), nullable=False),
        sa.Column("family", sa.Integer, nullable=False, server_default="4"),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_dhcp_exclusions_source", "dhcp_exclusions", ["source_type", "source_id"])


def downgrade() -> None:
    op.drop_index("ix_dhcp_exclusions_source", table_name="dhcp_exclusions")
    op.drop_table("dhcp_exclusions")
