"""Section（區段）— phpIPAM 對齊。"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, Sequence, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Section(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "sections"
    __table_args__ = (UniqueConstraint("compat_id", name="uq_sections_compat_id"),)

    name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sections.id", ondelete="SET NULL"),
        index=True,
    )
    strict_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 數字的相容 ID（0230）：phpIPAM 相容 API 用。Proxmox VE SDN 的 phpIPAM 外掛「區段」只收整數
    compat_id: Mapped[int] = mapped_column(Integer, Sequence("sections_compat_id_seq"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        index=True,
    )
