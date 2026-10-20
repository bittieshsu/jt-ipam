"""Hyper-V 整合（WinRM＋PowerShell；直連主機或經 SCVMM）

- 新增 hyperv_instances
- virtual_machines 加 os_name：客體作業系統（Hyper-V 的 KVP、SCVMM 的 OperatingSystem；其他平台之後也可以填）
- ip_addresses.discovery_source 加 'hyperv'：依 Hyper-V 回報自動建立的 IP 看得出是誰建的

Revision ID: 0222_hyperv
Revises: 0221_external_dns
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0222_hyperv"
down_revision: str | None = "0221_external_dns"
branch_labels: str | None = None
depends_on: str | None = None

_OLD = ("discovery_source IN ('manual','scanner','librenms','dns','proxmox','opnsense',"
        "'phpipam','pfsense','vmware','librenms_arp','isoinsight','snmp_arp')")
_NEW = ("discovery_source IN ('manual','scanner','librenms','dns','proxmox','opnsense',"
        "'phpipam','pfsense','vmware','librenms_arp','isoinsight','snmp_arp','hyperv')")
_NAMES = (
    "ip_discovery_source_valid",
    "ck_ip_addresses_ip_discovery_source_valid",
    "ck_ip_addresses_ck_ip_addresses_ip_discovery_source_valid",
)
_CANON = "ck_ip_addresses_ip_discovery_source_valid"


def upgrade() -> None:
    op.create_table(
        "hyperv_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        # hosts：逐台直連 Hyper-V 主機；scvmm：連 System Center VMM 伺服器
        sa.Column("mode", sa.String(16), nullable=False, server_default="hosts"),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("extra_hosts", sa.Text()),
        sa.Column("port", sa.Integer(), nullable=False, server_default="5985"),
        sa.Column("use_ssl", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("password_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True)),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("scope_subnet_ids", postgresql.JSONB()),
        sa.Column("auto_create_ips", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("mode IN ('hosts','scvmm')", name="ck_hyperv_instances_mode"),
    )
    op.add_column("virtual_machines", sa.Column("os_name", sa.String(255)))
    for n in _NAMES:
        op.execute(f'ALTER TABLE ip_addresses DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE ip_addresses ADD CONSTRAINT "{_CANON}" CHECK ({_NEW})')


def downgrade() -> None:
    op.execute("UPDATE ip_addresses SET discovery_source='scanner' WHERE discovery_source='hyperv'")
    for n in _NAMES:
        op.execute(f'ALTER TABLE ip_addresses DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE ip_addresses ADD CONSTRAINT "{_CANON}" CHECK ({_OLD})')
    op.drop_column("virtual_machines", "os_name")
    op.drop_table("hyperv_instances")
