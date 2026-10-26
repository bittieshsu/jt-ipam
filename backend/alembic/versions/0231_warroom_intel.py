"""戰情室 12 AI 情報：播報流與播報批次（2026-10-26）

一個可見範圍一條播報流（誰在看、下次該產生、產生中的租約）；播報批次產生完就不再改，
保留最新 20 批且最多 24 小時。見 app/models/warroom_intel.py、app/services/warroom/intel.py。

Revision ID: 0231_warroom_intel
Revises: 0230_section_compat_id
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0231_warroom_intel"
down_revision: str | None = "0230_section_compat_id"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "warroom_intel_streams",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("principal_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True)),
        sa.Column("next_due_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("manual_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_warroom_intel_streams_scope", "warroom_intel_streams", ["scope_key"], unique=True)
    op.create_index("ix_warroom_intel_streams_principal_user_id", "warroom_intel_streams", ["principal_user_id"])
    op.create_table(
        "warroom_intel_batches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("config_revision", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("model", sa.String(128)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("baseline", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("items", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("pinned", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("sources", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("stats", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_warroom_intel_batches_scope", "warroom_intel_batches", ["scope_key", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_warroom_intel_batches_scope", table_name="warroom_intel_batches")
    op.drop_table("warroom_intel_batches")
    op.drop_index("ix_warroom_intel_streams_principal_user_id", table_name="warroom_intel_streams")
    op.drop_index("uq_warroom_intel_streams_scope", table_name="warroom_intel_streams")
    op.drop_table("warroom_intel_streams")
