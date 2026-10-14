"""dns_compare_groups.compare_types：比對群組要比哪些紀錄類型（使用者 2026-10-14）

預設 A、AAAA、PTR（與以前的說明一致）；可另外勾 CNAME、MX、TXT、NS、SRV、CAA。
有勾其他類型的群組，成員拉取時會連那些類型一起存（dns_sync）。

Revision ID: 0214_dns_compare_types
Revises: 0213_lnms_vlan_domain_idx
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0214_dns_compare_types"
down_revision: str | None = "0213_lnms_vlan_domain_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dns_compare_groups", sa.Column(
        "compare_types", JSONB(), nullable=False, server_default=sa.text("'[\"A\", \"AAAA\", \"PTR\"]'::jsonb")))


def downgrade() -> None:
    op.drop_column("dns_compare_groups", "compare_types")
