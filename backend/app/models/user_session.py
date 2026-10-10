"""登入工作階段與 MFA 復原碼。

工作階段（2026-10-09 起）：每次登入建立一列，存的是更新權杖的雜湊（明文只在 HttpOnly Cookie 裡）。
- 存取權杖帶 `sid`，每個請求都確認這一列沒被撤銷 —— 登出、強制登出、停用帳號立即生效，
  不必等存取權杖 15 分鐘到期
- 更新權杖每次使用都換一把（`refresh_hash`），上一把留 60 秒寬限（`prev_refresh_hash`）給多個分頁
  同時換發；超過寬限還拿舊的來＝被偷用了，整個工作階段撤銷

復原碼：TOTP 裝置遺失時登入用，10 組、各只能用一次，只存 argon2 雜湊。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class UserSession(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prev_refresh_hash: Mapped[str | None] = mapped_column(String(64))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    #: 閒置到期：每次換發往後延（refresh_token_expire_days）
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))
    #: 登入方式（local / ldap / radius / oidc / saml）與是否通過 MFA
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    mfa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_user_sessions_active", "user_id", postgresql_where="revoked_at IS NULL"),
    )


class UserRecoveryCode(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "user_recovery_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
