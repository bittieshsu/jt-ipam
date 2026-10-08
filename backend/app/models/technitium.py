"""Technitium DNS Server 的 DHCP（使用者 2026-10-08：下一個要支援 Technitium，它有 DNS 與 DHCP）。

DNS 那一半是 DNSServer 的 `technitium` 類型（紀錄走共用的 dns_zones／dns_records）；DHCP 是這裡的獨立整合，
寫法比照 Kea：範圍、保留、租約寫進共用的 dhcp_pool_ranges／dhcp_reservations／dhcp_lease_sightings
（source_type = "technitium"）。

另外留一份範圍的鏡像（`technitium_dhcp_scopes`）：Technitium 明白給出每個範圍發給用戶端的預設閘道、DNS、
NTP、WINS —— 改址評估要知道「這個位址是某個範圍發出去的閘道或 DNS」（改了它，整個範圍的用戶端都會斷），
共用表沒有地方放這些選項。刪整合時跟著 CASCADE。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TechnitiumDhcpServer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "technitium_dhcp_servers"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Web 主控台的位址（預設 http://host:5380 或 https://host:53443）
    api_url: Mapped[str] = mapped_column(Text, nullable=False)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # API token（Technitium「建立 API token」產生，不會過期）。AES-GCM 雙欄加密，AAD 綁實例 id
    token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    sync_scopes: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)   # 範圍＋保留＋選項
    sync_leases: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)   # 租約

    # 限定子網路範圍（留空＝全域比對；重疊網段建議設定）
    scope_subnet_ids: Mapped[list[Any] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # 上一次同步看到什麼（版本、範圍數、租約數…），給畫面顯示
    last_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(Text)


class TechnitiumDhcpScope(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "technitium_dhcp_scopes"
    __table_args__ = (UniqueConstraint("server_id", "name", name="uq_technitium_scope_name"),)

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technitium_dhcp_servers.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    subnet_cidr: Mapped[str | None] = mapped_column(String(64))
    start_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    end_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    # Technitium 在這個範圍用哪個介面位址發 DHCP（scopes/list 的 interfaceAddress；非法 DHCP 偵測要放行）
    server_address: Mapped[str | None] = mapped_column(INET)
    # 從發放範圍挖掉的區間 [{"start": …, "end": …}]
    exclusions: Mapped[list[Any] | None] = mapped_column(JSONB)
    # 發給用戶端的選項（改址評估查「這個位址是不是某個範圍的閘道／DNS」）
    router: Mapped[str | None] = mapped_column(INET)
    dns_servers: Mapped[list[str] | None] = mapped_column(ARRAY(INET))
    ntp_servers: Mapped[list[str] | None] = mapped_column(ARRAY(INET))
    wins_servers: Mapped[list[str] | None] = mapped_column(ARRAY(INET))
    domain_name: Mapped[str | None] = mapped_column(String(255))
    lease_seconds: Mapped[int | None] = mapped_column(Integer)
    reservations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
