"""isoinsight：ISOinsight 整合（來源設定、來源租約觀察、同步記錄）（2026-10-07）

- isoinsight_sources：登入 ISOinsight 的 HTTP(S) 介面讀 DHCP 租約；密碼加密保存，Cookie／Token 不存
- isoinsight_leases：來源回報的租約（逐來源、逐 IP、逐 MAC），與正式 IP 分開；不依「這次沒出現」刪除
- isoinsight_sync_runs：測試／預覽／同步的記錄，計數是可查詢的欄位
- ip_addresses.discovery_source 加 'isoinsight'：依租約自動建立的 IP 看得出是誰建的

每個外鍵欄位都建索引（tests/test_fk_indexes.py）。

Revision ID: 0188_isoinsight
Revises: 0187_change_impact
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0188_isoinsight"
down_revision: str | None = "0187_change_impact"
branch_labels: str | None = None
depends_on: str | None = None

_OLD = ("discovery_source IN ('manual','scanner','librenms','dns','proxmox','opnsense',"
        "'phpipam','pfsense','vmware','librenms_arp')")
_NEW = ("discovery_source IN ('manual','scanner','librenms','dns','proxmox','opnsense',"
        "'phpipam','pfsense','vmware','librenms_arp','isoinsight')")
_NAMES = (
    "ip_discovery_source_valid",
    "ck_ip_addresses_ip_discovery_source_valid",
    "ck_ip_addresses_ck_ip_addresses_ip_discovery_source_valid",
)
_CANON = "ck_ip_addresses_ip_discovery_source_valid"


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                     server_default=sa.text("gen_random_uuid()"))


def upgrade() -> None:
    op.create_table(
        "isoinsight_sources",
        _uuid_pk(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("product_version", sa.String(128)),
        sa.Column("login_path", sa.String(255), nullable=False, server_default="/api/logon"),
        sa.Column("login_method", sa.String(8), nullable=False, server_default="POST"),
        sa.Column("post_format", sa.String(8), nullable=False, server_default="form"),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("password_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("username_param", sa.String(64), nullable=False, server_default="username"),
        sa.Column("password_param", sa.String(64), nullable=False, server_default="password"),
        sa.Column("auth_mode", sa.String(8), nullable=False, server_default="cookie"),
        sa.Column("token_path", sa.String(255)),
        sa.Column("token_header", sa.String(64), nullable=False, server_default="Authorization"),
        sa.Column("token_prefix", sa.String(32), nullable=False, server_default="Bearer"),
        sa.Column("lease_path", sa.String(255), nullable=False, server_default="/isosvc?act=DhcpLease"),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source_timezone", sa.String(64), nullable=False, server_default="Asia/Taipei"),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("scope_subnet_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False,
                  server_default=sa.text("'{}'::uuid[]")),
        sa.Column("create_ips", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("connect_timeout_seconds", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("job_timeout_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("max_response_mib", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("max_rows", sa.Integer(), nullable=False, server_default="100000"),
        sa.Column("description", sa.Text()),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_test_ok_at", sa.DateTime(timezone=True)),
        sa.Column("preview_ok_at", sa.DateTime(timezone=True)),
        sa.Column("auth_hold", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("retry_after_until", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_fetch_ok_at", sa.DateTime(timezone=True)),
        sa.Column("last_commit_at", sa.DateTime(timezone=True)),
        sa.Column("last_full_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_result", sa.String(16)),
        sa.Column("last_error_code", sa.String(48)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_summary", postgresql.JSONB()),
        sa.Column("running_since", sa.DateTime(timezone=True)),
        sa.Column("running_token", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("login_method IN ('GET','POST')", name="isoinsight_login_method"),
        sa.CheckConstraint("post_format IN ('form','json')", name="isoinsight_post_format"),
        sa.CheckConstraint("auth_mode IN ('cookie','token')", name="isoinsight_auth_mode"),
        sa.CheckConstraint("auth_mode <> 'token' OR token_path IS NOT NULL", name="isoinsight_token_path"),
        sa.CheckConstraint("sync_interval_seconds >= 60", name="isoinsight_interval_min"),
    )
    op.create_index("ix_isoinsight_sources_customer_id", "isoinsight_sources", ["customer_id"],
                    postgresql_where=sa.text("customer_id IS NOT NULL"))

    op.create_table(
        "isoinsight_leases",
        _uuid_pk(),
        sa.Column("source_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("isoinsight_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=False),
        sa.Column("mac_key", sa.String(12), nullable=False, server_default=""),
        sa.Column("mac", sa.String(17)),
        sa.Column("name", sa.String(255)),
        sa.Column("start_at", sa.DateTime(timezone=True)),
        sa.Column("end_at", sa.DateTime(timezone=True)),
        sa.Column("start_raw", sa.String(40)),
        sa.Column("end_raw", sa.String(40)),
        sa.Column("quality", postgresql.ARRAY(sa.String(24)), nullable=False,
                  server_default=sa.text("'{}'::varchar[]")),
        sa.Column("raw_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("match_status", sa.String(16), nullable=False, server_default="no_subnet"),
        sa.Column("subnet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("subnets.id", ondelete="SET NULL")),
        sa.Column("ip_address_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ip_addresses.id", ondelete="SET NULL")),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "ip", "mac_key", name="uq_isoinsight_lease"),
        sa.CheckConstraint("match_status IN ('matched','ambiguous','no_subnet')", name="isoinsight_match_status"),
    )
    op.create_index("ix_isoinsight_leases_source_id", "isoinsight_leases", ["source_id"])
    op.create_index("ix_isoinsight_leases_ip", "isoinsight_leases", ["ip"])
    op.create_index("ix_isoinsight_leases_mac", "isoinsight_leases", ["mac"])
    op.create_index("ix_isoinsight_leases_subnet_id", "isoinsight_leases", ["subnet_id"])
    op.create_index("ix_isoinsight_leases_ip_address_id", "isoinsight_leases", ["ip_address_id"])
    op.create_index("ix_isoinsight_leases_source_observed", "isoinsight_leases",
                    ["source_id", "lease_observed_at"])
    op.create_index("ix_isoinsight_leases_end_at", "isoinsight_leases", ["end_at"])

    op.create_table(
        "isoinsight_sync_runs",
        _uuid_pk(),
        sa.Column("source_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("isoinsight_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("background_task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("config_version", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("result", sa.String(16), nullable=False, server_default="running"),
        sa.Column("stage", sa.String(24)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_code", sa.String(48)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("login_method", sa.String(8)),
        sa.Column("auth_mode", sa.String(8)),
        *[sa.Column(c, sa.Integer(), nullable=False, server_default="0") for c in (
            "fetched", "valid", "invalid", "duplicates", "created", "updated", "unchanged", "observed_only",
            "expired", "unknown_time", "unmatched", "conflicts")],
        sa.Column("quality", postgresql.JSONB()),
        sa.Column("warnings", postgresql.ARRAY(sa.String(48))),
        sa.Column("stages", postgresql.JSONB()),
        sa.CheckConstraint("kind IN ('sync','test','preview')", name="isoinsight_run_kind"),
        sa.CheckConstraint("trigger IN ('manual','scheduled')", name="isoinsight_run_trigger"),
        sa.CheckConstraint("result IN ('running','success','partial','failed','skipped')",
                           name="isoinsight_run_result"),
    )
    op.create_index("ix_isoinsight_sync_runs_source_id", "isoinsight_sync_runs", ["source_id"])
    op.create_index("ix_isoinsight_sync_runs_actor_user_id", "isoinsight_sync_runs", ["actor_user_id"],
                    postgresql_where=sa.text("actor_user_id IS NOT NULL"))
    op.create_index("ix_isoinsight_sync_runs_source_started", "isoinsight_sync_runs",
                    ["source_id", "started_at"])

    for n in _NAMES:
        op.execute(f'ALTER TABLE ip_addresses DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE ip_addresses ADD CONSTRAINT "{_CANON}" CHECK ({_NEW})')


def downgrade() -> None:
    # 依租約建立的 IP 留著（正式資料），來源標記改回 manual，舊的 CHECK 才建得回去
    op.execute("UPDATE ip_addresses SET discovery_source='manual' WHERE discovery_source='isoinsight'")
    for n in _NAMES:
        op.execute(f'ALTER TABLE ip_addresses DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE ip_addresses ADD CONSTRAINT "{_CANON}" CHECK ({_OLD})')
    # 它寫進共用表的租約目擊與主機名稱回報（沒有外鍵，不會跟著表一起走）
    op.execute("DELETE FROM dhcp_lease_sightings WHERE source_type = 'isoinsight'")
    op.execute("DELETE FROM ip_hostname_reports WHERE source = 'isoinsight'")
    op.execute("DELETE FROM ip_hostname_observations WHERE source = 'isoinsight'")
    op.drop_table("isoinsight_sync_runs")
    op.drop_table("isoinsight_leases")
    op.drop_table("isoinsight_sources")
