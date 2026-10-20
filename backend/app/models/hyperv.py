"""Microsoft Hyper-V 整合（WinRM＋PowerShell，唯讀）。

兩種連法，同一張表：
- `hosts`：逐台直連 Hyper-V 主機（單機或容錯移轉叢集的每個節點）。NTLM 不能「二次跳轉」，
  連到 A 節點之後沒辦法再替使用者去問 B 節點，所以叢集的每個節點都要填（`extra_hosts`）。
- `scvmm`：連 System Center Virtual Machine Manager 伺服器，在那台上跑 VMM 的 PowerShell 模組，
  一次拿到它管理的所有主機與 VM（VMM 模組問的是本機的 VMM 服務，不需要二次跳轉）。

與 Proxmox、VMware 各自獨立設定、獨立同步，但寫進**同一組虛擬化資料表**
（`virt_clusters` / `virtual_machines` / `vm_interfaces`），拓樸、AI 對話、MCP 的 `list_vms` 不必改。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

HYPERV_MODES = ("hosts", "scvmm")


class HyperVInstance(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "hyperv_instances"
    __table_args__ = (
        CheckConstraint("mode IN ('hosts','scvmm')", name="ck_hyperv_instances_mode"),
        CheckConstraint("auth_method IN ('ntlm','kerberos')", name="ck_hyperv_instances_auth"),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="hosts", server_default="hosts")
    # hosts 模式：第一台主機；scvmm 模式：VMM 伺服器。主機名稱或 IP
    host: Mapped[str] = mapped_column(Text, nullable=False)
    # hosts 模式：同一個叢集的其他節點（換行或逗號分隔，每台都會連）；scvmm 模式：備援的 VMM 伺服器（依序試到通）
    extra_hosts: Mapped[str | None] = mapped_column(Text)
    port: Mapped[int] = mapped_column(Integer, default=5985, nullable=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)        # DOMAIN\user、.\user 或 user@domain
    # WinRM 驗證：ntlm 或 kerberos（0223；客戶的網域只開 Kerberos）。Kerberos 主機要填 FQDN，見 services/winrm_kerberos
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False, default="ntlm", server_default="ntlm")
    # 帳號寫 DOMAIN\user 時要填；寫 user@domain 可以留空
    kerberos_realm: Mapped[str | None] = mapped_column(String(255))
    # 留空＝照 AD 慣例查 DNS SRV；填了就用填的（換行或逗號分隔）
    kerberos_kdc: Mapped[str | None] = mapped_column(Text)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    password_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 對映到哪個叢集紀錄（第一次同步時建立，之後沿用）
    cluster_id: Mapped[uuid.UUID | None] = mapped_column()
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    # 限定子網路範圍：重疊網段下用來把 IP 比對縮到正確的子網路
    scope_subnet_ids: Mapped[list[Any] | None] = mapped_column(JSONB)
    # 信任虛擬化回報的 IP：IPAM 沒有該筆位址時自動建立。預設關閉（理由同 ESXi）
    auto_create_ips: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false"))
    description: Mapped[str | None] = mapped_column(Text)

    def host_list(self) -> list[str]:
        """主機（或 VMM 伺服器）清單：host 在前，extra_hosts 依序接在後面，去重。"""
        import re
        out: list[str] = []
        for h in [self.host, *re.split(r"[\s,]+", self.extra_hosts or "")]:
            h = (h or "").strip()
            if h and h not in out:
                out.append(h)
        return out
