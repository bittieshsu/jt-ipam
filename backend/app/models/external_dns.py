"""外部 DNS 解析的資料表（docs/SPEC_EXTERNAL_DNS_zh-TW.md；使用者 2026-10-19 定案）。

- `integration_own_addresses`：整合回報的設備自身介面位址（防火牆 WAN 等）。以前 iface_evidence 抓了只拿來替
  既有 IP 記錄蓋上線證據、沒有存下來，所以回答不了「這個公網 IP 是哪台防火牆的」。
- `external_dns_results`：每個名稱 × 每個解析器（或權威伺服器）最新一次的答案。
- `external_dns_changes`：公網解析變動的歷史（保留 90 天），給通知與追查。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class IntegrationOwnAddress(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "integration_own_addresses"

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)       # opnsense／pfsense／fortigate…
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    instance_name: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str] = mapped_column(INET, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("source_type", "source_id", "address", name="uq_integration_own_addresses"),)


class ExternalDnsResult(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "external_dns_results"

    fqdn: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    resolver: Mapped[str] = mapped_column(String(160), nullable=False)        # 解析器名稱或 authoritative:<NS>
    rcode: Mapped[str] = mapped_column(String(16), nullable=False)
    a: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    aaaa: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    cname: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    ttl: Mapped[int | None] = mapped_column(Integer)
    authoritative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("fqdn", "resolver", name="uq_external_dns_results"),)


class ExternalDnsChange(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "external_dns_changes"

    fqdn: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    old: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    new: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 index=True)
