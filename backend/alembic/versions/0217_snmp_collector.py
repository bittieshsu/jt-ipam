"""snmp_collector：掃描代理的 SNMP 收集（憑證、收集目標、收集記錄、LLDP／CDP 鄰居）（2026-10-15）

- snmp_credentials：v2c 社群字串／v3 帳密，機密以 AES-GCM 加密保存（AAD 綁列 id）；最多一組預設
- snmp_targets：收集目標＝既有裝置（device_id 唯一）、憑證（RESTRICT）、收集項目、收集者、管理位址覆寫
- snmp_runs：測試／收集的記錄（逐區段計數是 JSONB 欄位），每個目標只留最近一段
- snmp_neighbors：LLDP／CDP 鄰居
- fdb_entries.snmp_target_id：SNMP 讀到的交換器 MAC 表（source='snmp'），刪目標時跟著刪
- ip_addresses.discovery_source 加 'snmp_arp'：依路由器 ARP 自動建立的 IP 看得出是誰建的

每個外鍵欄位都建索引（tests/test_fk_indexes.py）。

Revision ID: 0217_snmp_collector
Revises: 0216_isoinsight_scope_all
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0217_snmp_collector"
down_revision: str | None = "0216_isoinsight_scope_all"
branch_labels: str | None = None
depends_on: str | None = None

_OLD = ("discovery_source IN ('manual','scanner','librenms','dns','proxmox','opnsense',"
        "'phpipam','pfsense','vmware','librenms_arp','isoinsight')")
_NEW = ("discovery_source IN ('manual','scanner','librenms','dns','proxmox','opnsense',"
        "'phpipam','pfsense','vmware','librenms_arp','isoinsight','snmp_arp')")
_NAMES = (
    "ip_discovery_source_valid",
    "ck_ip_addresses_ip_discovery_source_valid",
    "ck_ip_addresses_ck_ip_addresses_ip_discovery_source_valid",
)
_CANON = "ck_ip_addresses_ip_discovery_source_valid"


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                     server_default=sa.text("gen_random_uuid()"))


def _ts() -> list[sa.Column]:
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))]


def upgrade() -> None:
    op.create_table(
        "snmp_credentials",
        _uuid_pk(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("version", sa.String(8), nullable=False, server_default=sa.text("'v2c'")),
        sa.Column("community_enc", postgresql.BYTEA()),
        sa.Column("community_nonce", postgresql.BYTEA()),
        sa.Column("username", sa.String(128)),
        sa.Column("security_level", sa.String(16)),
        sa.Column("auth_protocol", sa.String(16)),
        sa.Column("auth_passphrase_enc", postgresql.BYTEA()),
        sa.Column("auth_passphrase_nonce", postgresql.BYTEA()),
        sa.Column("priv_protocol", sa.String(16)),
        sa.Column("priv_passphrase_enc", postgresql.BYTEA()),
        sa.Column("priv_passphrase_nonce", postgresql.BYTEA()),
        sa.Column("timeout_s", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("retries", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("description", sa.Text()),
        *_ts(),
        sa.CheckConstraint("version IN ('v2c','v3')", name="snmp_cred_version"),
        sa.CheckConstraint("security_level IS NULL OR security_level IN ('authPriv','authNoPriv')",
                           name="snmp_cred_level"),
        sa.CheckConstraint("timeout_s BETWEEN 1 AND 30", name="snmp_cred_timeout"),
        sa.CheckConstraint("retries BETWEEN 0 AND 5", name="snmp_cred_retries"),
    )
    op.create_index("ix_snmp_credentials_customer_id", "snmp_credentials", ["customer_id"])
    op.create_index("uq_snmp_credentials_default", "snmp_credentials", ["is_default"], unique=True,
                    postgresql_where=sa.text("is_default"))

    op.create_table(
        "snmp_targets",
        _uuid_pk(),
        sa.Column("device_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("credential_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("snmp_credentials.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ip_override", postgresql.INET()),
        sa.Column("port", sa.Integer(), nullable=False, server_default=sa.text("161")),
        sa.Column("collect_arp", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("collect_fdb", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("collect_neighbors", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("collect_interfaces", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("intervals", postgresql.JSONB()),
        sa.Column("collector", sa.String(8), nullable=False, server_default=sa.text("'auto'")),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("scan_agents.id", ondelete="SET NULL")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("auto_create_ips", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("description", sa.Text()),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_ok_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_summary", postgresql.JSONB()),
        *_ts(),
        sa.CheckConstraint("collector IN ('auto','agent','server')", name="snmp_target_collector"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="snmp_target_port"),
    )
    op.create_index("ix_snmp_targets_credential_id", "snmp_targets", ["credential_id"])
    op.create_index("ix_snmp_targets_agent_id", "snmp_targets", ["agent_id"])

    op.create_table(
        "snmp_runs",
        _uuid_pk(),
        sa.Column("target_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("snmp_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("scan_agents.id", ondelete="SET NULL")),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True)),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False, server_default=sa.text("'scheduled'")),
        sa.Column("result", sa.String(16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("sections", postgresql.JSONB()),
        sa.Column("info", postgresql.JSONB()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("kind IN ('test','collect')", name="snmp_run_kind"),
        sa.CheckConstraint("trigger IN ('scheduled','manual')", name="snmp_run_trigger"),
        sa.CheckConstraint("result IN ('running','success','partial','failed')", name="snmp_run_result"),
    )
    op.create_index("ix_snmp_runs_target_id", "snmp_runs", ["target_id"])
    op.create_index("ix_snmp_runs_agent_id", "snmp_runs", ["agent_id"])
    op.create_index("ix_snmp_runs_requested_by", "snmp_runs", ["requested_by"])
    op.create_index("ix_snmp_runs_target_started", "snmp_runs", ["target_id", "started_at"])

    op.create_table(
        "snmp_neighbors",
        _uuid_pk(),
        sa.Column("target_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("snmp_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("protocol", sa.String(8), nullable=False),
        sa.Column("local_port", sa.String(255)),
        sa.Column("remote_sysname", sa.String(255)),
        sa.Column("remote_port", sa.String(255)),
        sa.Column("remote_chassis_id", sa.String(255)),
        sa.Column("remote_mac", sa.String(32)),
        sa.Column("remote_mgmt_address", sa.String(64)),
        sa.Column("remote_platform", sa.String(255)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("protocol IN ('lldp','cdp')", name="snmp_neighbor_protocol"),
    )
    op.create_index("ix_snmp_neighbors_target_id", "snmp_neighbors", ["target_id"])
    op.create_index("ix_snmp_neighbors_device_id", "snmp_neighbors", ["device_id"])

    op.add_column("fdb_entries", sa.Column(
        "snmp_target_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("snmp_targets.id", ondelete="CASCADE")))
    op.create_index("ix_fdb_entries_snmp_target_id", "fdb_entries", ["snmp_target_id"])

    for n in _NAMES:
        op.execute(f'ALTER TABLE ip_addresses DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE ip_addresses ADD CONSTRAINT "{_CANON}" CHECK ({_NEW})')


def downgrade() -> None:
    op.execute("UPDATE ip_addresses SET discovery_source='scanner' WHERE discovery_source='snmp_arp'")
    for n in _NAMES:
        op.execute(f'ALTER TABLE ip_addresses DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE ip_addresses ADD CONSTRAINT "{_CANON}" CHECK ({_OLD})')
    op.drop_index("ix_fdb_entries_snmp_target_id", table_name="fdb_entries")
    op.drop_column("fdb_entries", "snmp_target_id")
    op.drop_table("snmp_neighbors")
    op.drop_table("snmp_runs")
    op.drop_table("snmp_targets")
    op.drop_table("snmp_credentials")
