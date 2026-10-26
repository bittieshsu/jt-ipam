"""戰情室 12 AI 情報：播報流與播報批次（0231）。見 services/warroom/intel.py。

- 播報流（一個可見範圍一列）：誰在看（最後看到的時間＋代表帳號）、下次該產生的時間、產生中的租約。
  同一個可見範圍的好幾台電視共用一份；不同權限的範圍分開（scope_key 含可見範圍雜湊）。
  租約讓多個 worker 同時收到請求時只有一個去問模型
- 播報批次：產生完就不再改（前端不拼接不同批次的數字與正文）；保留最新 20 批且最多 24 小時，取較小者
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class WarroomIntelStream(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "warroom_intel_streams"
    __table_args__ = (Index("uq_warroom_intel_streams_scope", "scope_key", unique=True),
                      Index("ix_warroom_intel_streams_principal_user_id", "principal_user_id"))

    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 用誰的權限產生（這個範圍最近一個觀看者；範圍雜湊相同代表看得到的東西相同）
    principal_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    #: 上次手動「立即產生」的時間（冷卻 60 秒）
    manual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WarroomIntelBatch(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "warroom_intel_batches"
    __table_args__ = (Index("ix_warroom_intel_batches_scope", "scope_key", "created_at"),)

    scope_key: Mapped[str] = mapped_column(String(64), nullable=False)
    config_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ready／partial／empty／error（error 只在第一批就失敗時出現；之後失敗保留上一批）
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: llm＝模型寫的；template＝確定性的系統摘要（沒有模型或模型兩次都不合格）
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    #: 首次建立或範圍改變：不報增減
    baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 最近一次重新檢查（輸入沒變時只更新這個，不改產生時間）
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    items: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    pinned: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    sources: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    #: 下一批比對用的快照（還開著的異常 id、各項數字）；不給前端
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
