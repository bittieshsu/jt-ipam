"""checkpoint_gaia：Check Point 第二階段，閘道的 Gaia API（DHCP 設定；選用：ARP 表、租約）

- checkpoint_gaia_targets：每台閘道的 Gaia API 連線（密碼 AES-GCM 雙欄加密），掛在管理伺服器底下
- checkpoint_dhcp_subnets：閘道 DHCP 伺服器的子網路設定鏡像（發給用戶端的閘道/DNS，改址評估用）
發放範圍、租約旗標、主機名稱走共用表（source_type／source = 'checkpoint'，source_id＝這張表的 id）。

Revision ID: 0195_checkpoint_gaia
Revises: 0194_checkpoint
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0195_checkpoint_gaia"
down_revision: str | None = "0194_checkpoint"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "checkpoint_gaia_targets",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("server_id", _UUID, sa.ForeignKey("checkpoint_servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain", sa.String(128), nullable=False, server_default=""),
        sa.Column("gateway_uid", sa.String(64)),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("gaia_url", sa.Text(), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("secret_enc", sa.LargeBinary()),
        sa.Column("secret_nonce", sa.LargeBinary()),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("sync_dhcp", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allow_scripts", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sync_arp", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_leases", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("scope_subnet_ids", postgresql.ARRAY(_UUID)),
        sa.Column("api_version", sa.String(16)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_summary", postgresql.JSONB()),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("server_id", "name", name="uq_checkpoint_gaia_target_name"),
    )
    op.create_index("ix_checkpoint_gaia_targets_server_id", "checkpoint_gaia_targets", ["server_id"])
    op.create_table(
        "checkpoint_dhcp_subnets",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("target_id", _UUID, sa.ForeignKey("checkpoint_gaia_targets.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("subnet_cidr", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("default_gateway", postgresql.INET()),
        sa.Column("dns_servers", postgresql.ARRAY(postgresql.INET())),
        sa.Column("domain_name", sa.String(255)),
        sa.Column("default_lease", sa.Integer()),
        sa.Column("max_lease", sa.Integer()),
        sa.Column("pools", postgresql.JSONB()),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("target_id", "subnet_cidr", name="uq_checkpoint_dhcp_subnet"),
    )
    op.create_index("ix_checkpoint_dhcp_subnets_target_id", "checkpoint_dhcp_subnets", ["target_id"])


def downgrade() -> None:
    op.execute("DELETE FROM dhcp_pool_ranges WHERE source_type = 'checkpoint'")
    op.execute("DELETE FROM dhcp_reservations WHERE source_type = 'checkpoint'")
    op.execute("DELETE FROM dhcp_lease_sightings WHERE source_type = 'checkpoint'")
    op.execute("DELETE FROM ip_hostname_reports WHERE source = 'checkpoint'")
    op.execute("DELETE FROM ip_hostname_observations WHERE source = 'checkpoint'")
    op.drop_index("ix_checkpoint_dhcp_subnets_target_id", table_name="checkpoint_dhcp_subnets")
    op.drop_table("checkpoint_dhcp_subnets")
    op.drop_index("ix_checkpoint_gaia_targets_server_id", table_name="checkpoint_gaia_targets")
    op.drop_table("checkpoint_gaia_targets")
