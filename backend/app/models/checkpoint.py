"""Check Point 整合的資料模型（使用者 2026-10-08：比照 Palo Alto／FortiGate，R81.20）。

**與 PA／FG 最大的不同：設定在管理伺服器，不在防火牆。** 整合的單位是一台管理伺服器
（Security Management Server、Multi-Domain Server 或 Smart-1 Cloud），它管很多台閘道與很多套政策套件。

- 第一階段（這裡）：Management API（`/web_api/<指令>`，POST JSON，登入後帶 `X-chkp-sid`）唯讀抓
  閘道、位址物件、存取規則（政策套件 → 存取層 → 規則，含段落與內嵌層）、NAT
- 第二階段（待 VM）：閘道的 Gaia API 拿 ARP、DHCP 租約、VPN 狀態

Multi-Domain 每個網域各自登入；物件名稱在網域內唯一，所以鏡像表都帶 `domain`（單一管理伺服器是空字串）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CheckPointServer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一台 Check Point 管理伺服器。"""

    __tablename__ = "checkpoint_servers"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # https://<管理伺服器>；Smart-1 Cloud 是 https://<租戶>.maas.checkpoint.com/<context>（後面的 /web_api 自動補）
    api_url: Mapped[str] = mapped_column(Text, nullable=False)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # api_key（建議，R80.40 起）或 password（搭配 username）
    auth_mode: Mapped[str] = mapped_column(String(16), default="api_key", nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    # API key 或密碼，AES-GCM 雙欄加密，AAD 綁實例 id
    secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Multi-Domain 要同步的網域（空＝單一管理伺服器，不帶 domain 登入）
    domains: Mapped[list[str] | None] = mapped_column(ARRAY(String(128)))
    # 只同步這些政策套件（空＝全部）
    packages: Mapped[list[str] | None] = mapped_column(ARRAY(String(128)))

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 管理伺服器比防火牆吃重：預設 15 分鐘一輪
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    sync_objects: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_policies: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_nat: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scope_subnet_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))

    # 偵測到的 Management API 版本（R81.20 是 1.9）
    api_version: Mapped[str | None] = mapped_column(String(16))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("auth_mode IN ('api_key','password')", name="ck_checkpoint_auth_mode"),)


class CheckPointGateway(Base, UUIDPrimaryKeyMixin):
    """管理伺服器管的閘道與伺服器（show-gateways-and-servers）。"""

    __tablename__ = "checkpoint_gateways"
    __table_args__ = (UniqueConstraint("server_id", "domain", "uid", name="uq_checkpoint_gateway"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("checkpoint_servers.id", ondelete="CASCADE"), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    gw_type: Mapped[str | None] = mapped_column(String(64))
    ipv4_address: Mapped[str | None] = mapped_column(INET)
    version: Mapped[str | None] = mapped_column(String(32))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CheckPointObject(Base, UUIDPrimaryKeyMixin):
    """位址物件：host／network／address-range／group／group-with-exclusion。"""

    __tablename__ = "checkpoint_objects"
    __table_args__ = (UniqueConstraint("server_id", "domain", "uid", name="uq_checkpoint_object"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("checkpoint_servers.id", ondelete="CASCADE"), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    obj_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # host：位址；network：CIDR；address-range：起-迄；group-with-exclusion：「include − except」的名稱
    value: Mapped[str | None] = mapped_column(Text)
    # group：成員名稱
    members: Mapped[list[str] | None] = mapped_column(JSONB)
    comments: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CheckPointRule(Base, UUIDPrimaryKeyMixin):
    """存取規則（政策套件 → 存取層 → 規則；段落攤平，內嵌層的層名寫成「上層 › 內嵌層」）。"""

    __tablename__ = "checkpoint_rules"
    __table_args__ = (UniqueConstraint("server_id", "domain", "layer", "uid", name="uq_checkpoint_rule"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("checkpoint_servers.id", ondelete="CASCADE"), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    package: Mapped[str] = mapped_column(String(255), nullable=False)
    layer: Mapped[str] = mapped_column(String(512), nullable=False)
    section: Mapped[str | None] = mapped_column(String(255))
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_number: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 名稱以逗號分隔（Any 存成 any，跟其他廠牌一樣）
    source: Mapped[str | None] = mapped_column(Text)
    destination: Mapped[str | None] = mapped_column(Text)
    service: Mapped[str | None] = mapped_column(Text)
    source_negate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    destination_negate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    install_on: Mapped[str | None] = mapped_column(Text)
    comments: Mapped[str | None] = mapped_column(Text)
    hits: Mapped[int | None] = mapped_column(Integer)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__: list[Any] = ["CheckPointGateway", "CheckPointObject", "CheckPointRule", "CheckPointServer"]
