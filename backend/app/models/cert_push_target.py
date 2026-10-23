"""憑證推送目標（0227）：伺服器主動把憑證推到防火牆，不需要在對方裝代理。

使用者 2026-10-22：「派送憑證去 fg pa ck opn pf」。防火牆裝不了我們的派送代理，所以改由 jt-ipam 透過
各家的管理 API 推：匯入憑證 → 換上（綁到管理介面、SSL VPN、服務設定檔，或直接替換既有憑證的內容）→
連過去確認對方真的換成新憑證，換不上就換回原來的（services/cert_push）。

- 帳密是**另外一組**、只給換憑證需要的權限（既有的整合帳號多半是唯讀），存在 encrypted_secrets
  （object_type='cert_push_target'，AAD 綁目標 id）。
- 各家的換上方式放在 options（例如 FortiGate 綁哪些服務、OPNsense 替換哪一張既有憑證、Palo Alto 的服務設定檔）。
- auto_push：憑證有新版時由定期同步自動推（目標上的指紋與目前版本不同才推）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: 支援的設備種類（drivers 在 services/cert_push/）
PUSH_KINDS = ("fortigate", "opnsense", "paloalto", "pfsense")


class CertPushTarget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "cert_push_targets"

    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    certificate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("certificates.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    api_url: Mapped[str] = mapped_column(String(512), nullable=False)
    verify_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: 各家的換上方式（見 services/cert_push/drivers 各檔頂端說明）
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    #: 推完要連過去確認的位址（host:port）；空的＝用 api_url 的主機與埠
    verify_endpoint: Mapped[str | None] = mapped_column(String(300))
    auto_push: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    last_push_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: ok／unverified（推上去但連不到確認）／error／rolled_back
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    #: 目前推上去、確認過的版本指紋（auto_push 用它判斷要不要再推）
    last_fingerprint: Mapped[str | None] = mapped_column(String(64))
    #: 上一次推送的步驟記錄（給畫面顯示做了什麼、在哪一步失敗）
    last_steps: Mapped[list[Any] | None] = mapped_column(JSONB)
