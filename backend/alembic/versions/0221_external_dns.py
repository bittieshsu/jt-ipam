"""外部 DNS 解析：設備自身位址、解析結果、變動歷史（使用者 2026-10-19 定案；規格 docs/SPEC_EXTERNAL_DNS_zh-TW.md）

設定存在 system_settings（key dns_resolvers），不需要表。

Revision ID: 0221_external_dns
Revises: 0220_bogus_mac_cleanup
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

revision: str = "0221_external_dns"
down_revision: str | None = "0220_bogus_mac_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_own_addresses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("instance_name", sa.Text()),
        sa.Column("address", INET(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_type", "source_id", "address", name="uq_integration_own_addresses"),
    )
    op.create_index("ix_integration_own_addresses_address", "integration_own_addresses", ["address"])
    op.create_table(
        "external_dns_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("fqdn", sa.Text(), nullable=False),
        sa.Column("resolver", sa.String(160), nullable=False),
        sa.Column("rcode", sa.String(16), nullable=False),
        sa.Column("a", JSONB(), nullable=False, server_default="[]"),
        sa.Column("aaaa", JSONB(), nullable=False, server_default="[]"),
        sa.Column("cname", JSONB(), nullable=False, server_default="[]"),
        sa.Column("ttl", sa.Integer()),
        sa.Column("authoritative", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("fqdn", "resolver", name="uq_external_dns_results"),
    )
    op.create_index("ix_external_dns_results_fqdn", "external_dns_results", ["fqdn"])
    op.create_table(
        "external_dns_changes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("fqdn", sa.Text(), nullable=False),
        sa.Column("old", JSONB(), nullable=False, server_default="[]"),
        sa.Column("new", JSONB(), nullable=False, server_default="[]"),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_external_dns_changes_fqdn", "external_dns_changes", ["fqdn"])
    op.create_index("ix_external_dns_changes_changed_at", "external_dns_changes", ["changed_at"])


def downgrade() -> None:
    op.drop_table("external_dns_changes")
    op.drop_table("external_dns_results")
    op.drop_table("integration_own_addresses")
