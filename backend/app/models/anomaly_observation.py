"""異常觀測：每一筆異常出現到消失的期間（0224）。見 services/anomaly_history.py。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class AnomalyObservation(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "anomaly_observations"
    __table_args__ = (
        Index("uq_anomaly_obs_open", "category", "fingerprint", unique=True,
              postgresql_where=text("resolved_at IS NULL")),
        Index("ix_anomaly_obs_first_seen", "first_seen_at"),
        Index("ix_anomaly_obs_resolved", "resolved_at"),
        Index("ix_anomaly_obs_subnet", "subnet_id"),
    )

    category: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))
    ip: Mapped[str | None] = mapped_column(INET)
    subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subnets.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 觀測表第一次記錄時就已經存在（0225）：真正的首次出現時間不知道，不可以當成那一刻才新出現
    baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
