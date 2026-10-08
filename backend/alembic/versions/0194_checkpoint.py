"""checkpoint：Check Point 管理伺服器整合（第一階段：Management API，唯讀）

- checkpoint_servers：管理伺服器設定（API key 或密碼以 AES-GCM 雙欄加密）
- checkpoint_gateways／checkpoint_objects／checkpoint_rules：閘道、位址物件、存取規則的鏡像
NAT 寫進共用的 nat_translations（source_origin = checkpoint:<id>）。

Revision ID: 0194_checkpoint
Revises: 0193_impact_ai_stage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0194_checkpoint"
down_revision: str | None = "0193_impact_ai_stage"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)


def _fk() -> sa.Column:
    return sa.Column("server_id", _UUID, sa.ForeignKey("checkpoint_servers.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "checkpoint_servers",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("api_url", sa.Text(), nullable=False),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auth_mode", sa.String(16), nullable=False, server_default="api_key"),
        sa.Column("username", sa.String(255)),
        sa.Column("secret_enc", sa.LargeBinary()),
        sa.Column("secret_nonce", sa.LargeBinary()),
        sa.Column("domains", postgresql.ARRAY(sa.String(128))),
        sa.Column("packages", postgresql.ARRAY(sa.String(128))),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("sync_objects", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_policies", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_nat", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("scope_subnet_ids", postgresql.ARRAY(_UUID)),
        sa.Column("api_version", sa.String(16)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_summary", postgresql.JSONB()),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("auth_mode IN ('api_key','password')", name="ck_checkpoint_auth_mode"),
    )
    op.create_table(
        "checkpoint_gateways",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _fk(),
        sa.Column("domain", sa.String(128), nullable=False, server_default=""),
        sa.Column("uid", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("gw_type", sa.String(64)),
        sa.Column("ipv4_address", postgresql.INET()),
        sa.Column("version", sa.String(32)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("server_id", "domain", "uid", name="uq_checkpoint_gateway"),
    )
    op.create_table(
        "checkpoint_objects",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _fk(),
        sa.Column("domain", sa.String(128), nullable=False, server_default=""),
        sa.Column("uid", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("obj_type", sa.String(32), nullable=False),
        sa.Column("value", sa.Text()),
        sa.Column("members", postgresql.JSONB()),
        sa.Column("comments", sa.Text()),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("server_id", "domain", "uid", name="uq_checkpoint_object"),
    )
    op.create_table(
        "checkpoint_rules",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _fk(),
        sa.Column("domain", sa.String(128), nullable=False, server_default=""),
        sa.Column("package", sa.String(255), nullable=False),
        sa.Column("layer", sa.String(512), nullable=False),
        sa.Column("section", sa.String(255)),
        sa.Column("uid", sa.String(64), nullable=False),
        sa.Column("rule_number", sa.Integer()),
        sa.Column("name", sa.String(255)),
        sa.Column("action", sa.String(64)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.Text()),
        sa.Column("destination", sa.Text()),
        sa.Column("service", sa.Text()),
        sa.Column("source_negate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("destination_negate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("install_on", sa.Text()),
        sa.Column("comments", sa.Text()),
        sa.Column("hits", sa.Integer()),
        sa.Column("last_hit_at", sa.DateTime(timezone=True)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("server_id", "domain", "layer", "uid", name="uq_checkpoint_rule"),
    )
    for t in ("checkpoint_gateways", "checkpoint_objects", "checkpoint_rules"):
        op.create_index(f"ix_{t}_server_id", t, ["server_id"])


def downgrade() -> None:
    op.execute("DELETE FROM nat_translations WHERE source_origin LIKE 'checkpoint:%'")
    for t in ("checkpoint_rules", "checkpoint_objects", "checkpoint_gateways"):
        op.drop_index(f"ix_{t}_server_id", table_name=t)
        op.drop_table(t)
    op.drop_table("checkpoint_servers")
