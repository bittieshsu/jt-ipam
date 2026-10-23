"""憑證推送目標：伺服器主動把憑證推到防火牆（FortiGate、OPNsense、Palo Alto、pfSense）（2026-10-22）

Revision ID: 0227_cert_push_targets
Revises: 0226_fw_snapshot_auto_rules
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0227_cert_push_targets"
down_revision: str | None = "0226_fw_snapshot_auto_rules"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "cert_push_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("certificate_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("certificates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("api_url", sa.String(512), nullable=False),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("verify_endpoint", sa.String(300)),
        sa.Column("auto_push", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_push_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", sa.String(16)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_fingerprint", sa.String(64)),
        sa.Column("last_steps", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_cert_push_targets_certificate_id", "cert_push_targets", ["certificate_id"])


def downgrade() -> None:
    op.drop_index("ix_cert_push_targets_certificate_id", table_name="cert_push_targets")
    op.drop_table("cert_push_targets")
