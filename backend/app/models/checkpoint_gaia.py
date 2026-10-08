"""Check Point 第二階段：閘道的 Gaia API（使用者 2026-10-08：要 DHCP 租約、ARP 表之類）。

第一階段讀的是管理伺服器；ARP、DHCP 是閘道自己的作業系統（Gaia）上的東西，要連閘道的 Gaia API
（`https://<閘道>/gaia_api/`，帳密登入，跟管理伺服器的 API key 是兩套帳號）。

- 一筆＝一台閘道的 Gaia 連線，掛在某台管理伺服器底下。`gateway_uid` 對到第一階段同步回來的閘道
  （只是對照，不是外鍵：管理伺服器那邊閘道清單暫時讀不到時，這裡的帳密不能跟著被刪）
- 唯讀（`sync_dhcp`，預設開）：`show-dhcp-server` → 發放範圍與發給用戶端的閘道/DNS
- 選用（`allow_scripts`，預設關）：Gaia API 沒有讀 ARP 或租約的指令，只能用 `run-script` 跑
  **寫死的**讀表／讀檔指令。那需要能執行指令的帳號（不是唯讀角色），所以要管理員明確打開
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
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


class CheckPointGaiaTarget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一台閘道的 Gaia API 連線。"""

    __tablename__ = "checkpoint_gaia_targets"
    __table_args__ = (UniqueConstraint("server_id", "name", name="uq_checkpoint_gaia_target_name"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("checkpoint_servers.id", ondelete="CASCADE"), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    gateway_uid: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # https://<閘道>/gaia_api（後面的 /gaia_api 沒寫會自動補）
    gaia_url: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    # 密碼，AES-GCM 雙欄加密，AAD 綁這一筆的 id
    secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    sync_dhcp: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 允許執行寫死的讀取指令（ARP 表、租約檔）；要能執行指令的帳號
    allow_scripts: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sync_arp: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_leases: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 空＝沿用管理伺服器的限定子網路範圍
    scope_subnet_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))

    api_version: Mapped[str | None] = mapped_column(String(16))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(Text)


class CheckPointDhcpSubnet(Base, UUIDPrimaryKeyMixin):
    """閘道 DHCP 伺服器的子網路設定鏡像（show-dhcp-server）：發放範圍另外寫進共用的 dhcp_pool_ranges，
    這裡留下發給用戶端的閘道/DNS，給 IP 變更評估查「這個位址是哪個 DHCP 子網路發出去的閘道或 DNS」。"""

    __tablename__ = "checkpoint_dhcp_subnets"
    __table_args__ = (UniqueConstraint("target_id", "subnet_cidr", name="uq_checkpoint_dhcp_subnet"),)

    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("checkpoint_gaia_targets.id", ondelete="CASCADE"), nullable=False, index=True)
    subnet_cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_gateway: Mapped[str | None] = mapped_column(INET)
    dns_servers: Mapped[list[str] | None] = mapped_column(ARRAY(INET))
    domain_name: Mapped[str | None] = mapped_column(String(255))
    default_lease: Mapped[int | None] = mapped_column(Integer)
    max_lease: Mapped[int | None] = mapped_column(Integer)
    # 原始的 ip-pools（含排除與停用的），畫面顯示用
    pools: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__: list[Any] = ["CheckPointDhcpSubnet", "CheckPointGaiaTarget"]
