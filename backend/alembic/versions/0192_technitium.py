"""technitium：Technitium DNS Server（DNS 與 DHCP）

- dns_servers.type 加 'technitium'（DNS 紀錄走既有的 dns_zones／dns_records）
- technitium_dhcp_servers：DHCP 整合（比照 kea_dhcp_servers；token AES-GCM 雙欄加密）
- technitium_dhcp_scopes：範圍鏡像，含發給用戶端的閘道／DNS／NTP／WINS（改址評估用）

Revision ID: 0192_technitium
Revises: 0191_isoinsight_schedule_on
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0192_technitium"
down_revision: str | None = "0191_isoinsight_schedule_on"
branch_labels = None
depends_on = None

_OLD = "type IN ('powerdns','bind9','unbound_opnsense','windows_dns','univention_ucs')"
_NEW = "type IN ('powerdns','bind9','unbound_opnsense','windows_dns','univention_ucs','technitium')"


def upgrade() -> None:
    op.drop_constraint("ck_dns_servers_type_valid", "dns_servers", type_="check")
    op.create_check_constraint("ck_dns_servers_type_valid", "dns_servers", _NEW)

    op.create_table(
        "technitium_dhcp_servers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("api_url", sa.Text(), nullable=False),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("token_enc", sa.LargeBinary()),
        sa.Column("token_nonce", sa.LargeBinary()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("sync_scopes", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_leases", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("scope_subnet_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_summary", postgresql.JSONB()),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "technitium_dhcp_scopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("server_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("technitium_dhcp_servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("subnet_cidr", sa.String(64)),
        sa.Column("start_ip", sa.String(64), nullable=False),
        sa.Column("end_ip", sa.String(64), nullable=False),
        sa.Column("server_address", postgresql.INET()),
        sa.Column("exclusions", postgresql.JSONB()),
        sa.Column("router", postgresql.INET()),
        sa.Column("dns_servers", postgresql.ARRAY(postgresql.INET())),
        sa.Column("ntp_servers", postgresql.ARRAY(postgresql.INET())),
        sa.Column("wins_servers", postgresql.ARRAY(postgresql.INET())),
        sa.Column("domain_name", sa.String(255)),
        sa.Column("lease_seconds", sa.Integer()),
        sa.Column("reservations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("server_id", "name", name="uq_technitium_scope_name"),
    )
    op.create_index("ix_technitium_dhcp_scopes_server_id", "technitium_dhcp_scopes", ["server_id"])


def downgrade() -> None:
    op.execute("DELETE FROM dhcp_pool_ranges WHERE source_type = 'technitium'")
    op.execute("DELETE FROM dhcp_reservations WHERE source_type = 'technitium'")
    op.execute("DELETE FROM dhcp_lease_sightings WHERE source_type = 'technitium'")
    op.execute("DELETE FROM ip_hostname_reports WHERE source = 'technitium'")
    op.execute("DELETE FROM ip_hostname_observations WHERE source = 'technitium'")
    op.drop_index("ix_technitium_dhcp_scopes_server_id", table_name="technitium_dhcp_scopes")
    op.drop_table("technitium_dhcp_scopes")
    op.drop_table("technitium_dhcp_servers")
    op.execute("DELETE FROM dns_servers WHERE type = 'technitium'")
    op.drop_constraint("ck_dns_servers_type_valid", "dns_servers", type_="check")
    op.create_check_constraint("ck_dns_servers_type_valid", "dns_servers", _OLD)
