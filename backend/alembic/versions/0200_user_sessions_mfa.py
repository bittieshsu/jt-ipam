"""user_sessions_mfa：伺服器端可撤銷的登入工作階段、MFA 復原碼與防重放

2026-10-09 合規核對：
- 更新權杖是不受追蹤的 JWT，14 天內一直有效；登出什麼都不做，改密碼也收不回已發出的權杖。
  → user_sessions：每次登入一列、存更新權杖的雜湊；存取權杖帶 sid，每個請求都確認沒被撤銷
- users.tokens_valid_after：強制登出／停用／管理員重設密碼時設定，早於它簽發的存取權杖一律無效
- users.totp_last_step：同一組 TOTP 驗證碼不能用第二次
- user_recovery_codes：TOTP 裝置遺失時登入用（各用一次，只存雜湊）

升級後既有的登入（舊格式的更新權杖）需要重新登入一次。

Revision ID: 0200_user_sessions_mfa
Revises: 0199_token_expiry_and_cleanup
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0200_user_sessions_mfa"
down_revision: str | None = "0199_token_expiry_and_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("tokens_valid_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("refresh_hash", sa.String(64), nullable=False),
        sa.Column("prev_refresh_hash", sa.String(64), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(64), nullable=True),
        sa.Column("method", sa.String(16), nullable=False, server_default="local"),
        sa.Column("mfa", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.UniqueConstraint("refresh_hash", name="uq_user_sessions_refresh_hash"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_active", "user_sessions", ["user_id"],
                    postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_table(
        "user_recovery_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_user_recovery_codes_user_id", "user_recovery_codes", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_recovery_codes")
    op.drop_table("user_sessions")
    op.drop_column("users", "totp_last_step")
    op.drop_column("users", "tokens_valid_after")
