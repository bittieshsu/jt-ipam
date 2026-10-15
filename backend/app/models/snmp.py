"""SNMP 收集（migration 0217）：掃描代理對「伺服器指派的網路設備」做唯讀 SNMP 讀取。

起因（客戶 2026-10-15）：掃描代理隔著路由器掃另一個網段，ping 得到卻沒有 MAC（沒有 L2、也沒有
LibreNMS）。路由器的 ARP 表、交換器的 MAC 表、LLDP／CDP 鄰居、介面清單都在設備上，只差有人去讀。

設計重點（memory: project_snmp_collector_design）：
- **憑證集中**（`snmp_credentials`）：社群字串／v3 帳密不在每台裝置上填；換代理也不必重填
- **收集目標＝既有裝置**（`snmp_targets.device_id` 唯一）：管理位址取裝置的主要 IP，必要時另填覆寫
- **收集者**：auto＝管理位址所在子網路的負責代理、agent＝指定代理、server＝本機代理（is_local）
- 機密走 AES-GCM，AAD 綁這一列的 id（比照其他整合）；API 永遠不回傳，只回 has_*
- 收集記錄（`snmp_runs`）只留最近一段（每個目標最多 RUN_KEEP_PER_TARGET 筆、RUN_KEEP_DAYS 天）
- FDB 寫進共用的 `fdb_entries`（source='snmp'、switch_device_id＝目標裝置、snmp_target_id）
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: v3 的認證協定（net-snmp 的 defAuthType 名稱）。MD5 可選但畫面標「不建議」
AUTH_PROTOCOLS = ("MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512")
#: v3 的加密協定。DES 可選但畫面標「不建議」；AES-128 在 net-snmp 叫 AES
PRIV_PROTOCOLS = ("DES", "AES-128", "AES-192", "AES-256")
#: 安全等級：刻意不開放 noAuthNoPriv（等於沒有驗證，比 v2c 好不到哪去，卻讓人以為用了 v3 很安全）
SECURITY_LEVELS = ("authPriv", "authNoPriv")
VERSIONS = ("v2c", "v3")
COLLECTORS = ("auto", "agent", "server")
#: 收集項目（區段）
SECTIONS = ("arp", "fdb", "neighbors", "interfaces")
#: 各區段預設間隔（秒）與下限：路由器的 ARP 變化快、介面與鄰居幾乎不動
DEFAULT_INTERVALS = {"arp": 300, "fdb": 900, "neighbors": 3600, "interfaces": 3600}
MIN_INTERVALS = {"arp": 60, "fdb": 300, "neighbors": 900, "interfaces": 900}

RUN_KEEP_PER_TARGET = 300
RUN_KEEP_DAYS = 14


class SnmpCredential(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一組 SNMP 憑證（v2c 社群字串，或 v3 帳號＋認證／加密）。"""

    __tablename__ = "snmp_credentials"

    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    #: 預設憑證（最多一組；部分唯一索引）。新增收集目標沒指定憑證時用它
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: 單位（選用）：設了就只能指派給該單位的裝置
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True)
    version: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'v2c'"))

    # v2c：社群字串（網路上是明文傳送，畫面要警告）
    community_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    community_nonce: Mapped[bytes | None] = mapped_column(BYTEA)

    # v3（USM）
    username: Mapped[str | None] = mapped_column(String(128))
    security_level: Mapped[str | None] = mapped_column(String(16))
    auth_protocol: Mapped[str | None] = mapped_column(String(16))
    auth_passphrase_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    auth_passphrase_nonce: Mapped[bytes | None] = mapped_column(BYTEA)
    priv_protocol: Mapped[str | None] = mapped_column(String(16))
    priv_passphrase_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    priv_passphrase_nonce: Mapped[bytes | None] = mapped_column(BYTEA)

    #: 進階：單一請求的逾時（秒）與重試次數（交給 net-snmp 的 -t / -r）
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("version IN ('v2c','v3')", name="snmp_cred_version"),
        CheckConstraint("security_level IS NULL OR security_level IN ('authPriv','authNoPriv')",
                        name="snmp_cred_level"),
        CheckConstraint("timeout_s BETWEEN 1 AND 30", name="snmp_cred_timeout"),
        CheckConstraint("retries BETWEEN 0 AND 5", name="snmp_cred_retries"),
        Index("uq_snmp_credentials_default", "is_default", unique=True,
              postgresql_where=text("is_default")),
    )


class SnmpTarget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一台要收集的網路設備（＝既有裝置）。"""

    __tablename__ = "snmp_targets"

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, unique=True)
    #: 憑證被收集目標用著就不能刪（RESTRICT；API 先回 409 並列出使用中的裝置）
    credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snmp_credentials.id", ondelete="RESTRICT"), nullable=False, index=True)
    #: 管理位址覆寫：空＝用裝置的主要 IP（IP 變更評估要讀這一欄：它是連線位址）
    ip_override: Mapped[str | None] = mapped_column(INET)
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("161"))

    collect_arp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    collect_fdb: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    collect_neighbors: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    collect_interfaces: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: 各區段間隔覆寫（秒）：{"arp": 300, ...}；空＝預設（DEFAULT_INTERVALS），低於 MIN_INTERVALS 一律拉回下限
    intervals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    collector: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'auto'"))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_agents.id", ondelete="SET NULL"), index=True)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: 路由器 ARP 看到 IPAM 沒有的位址時自動建立（比照 LibreNMS #48，預設關）
    auto_create_ips: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    description: Mapped[str | None] = mapped_column(Text)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    #: 最後錯誤的代碼（前端 errors.<code> 翻譯；last_error 是退路的原文）
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    #: {"system": {sys_name, sys_descr, sys_object_id, sys_uptime},
    #:  "sections": {"arp": {"status", "rows", "at", "error", ...}, ...}}
    last_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("collector IN ('auto','agent','server')", name="snmp_target_collector"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="snmp_target_port"),
    )


class SnmpRun(Base, UUIDPrimaryKeyMixin):
    """一次測試或收集的記錄（計數是欄位，不是一段文字）。"""

    __tablename__ = "snmp_runs"

    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snmp_targets.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_agents.id", ondelete="SET NULL"), index=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True)
    #: 走代理工作通道的那一筆（agent_probe_jobs.id）；工作記錄會被清，不設外鍵
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(8), nullable=False)          # test / collect
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'scheduled'"))
    result: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'running'"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    #: 逐區段：{"arp": {"status": "ok", "rows": 1234, "matched": 900, "elapsed_ms": 812}, ...}
    sections: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: 測試的逐組憑證結果、系統資訊（sysName / sysDescr / sysObjectID / sysUpTime）
    info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("kind IN ('test','collect')", name="snmp_run_kind"),
        CheckConstraint("trigger IN ('scheduled','manual')", name="snmp_run_trigger"),
        CheckConstraint("result IN ('running','success','partial','failed')", name="snmp_run_result"),
        Index("ix_snmp_runs_target_started", "target_id", "started_at"),
    )


class SnmpNeighbor(Base, UUIDPrimaryKeyMixin):
    """LLDP／CDP 鄰居：對方自己宣告「我是誰、接在你哪個埠」。每輪完整讀到才整批換掉。"""

    __tablename__ = "snmp_neighbors"

    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snmp_targets.id", ondelete="CASCADE"), nullable=False, index=True)
    #: 目標裝置（反正規化：拓樸與裝置詳情頁直接用，不必再經過 snmp_targets）
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False)       # lldp / cdp
    local_port: Mapped[str | None] = mapped_column(String(255))
    remote_sysname: Mapped[str | None] = mapped_column(String(255))
    remote_port: Mapped[str | None] = mapped_column(String(255))
    #: LLDP 的 chassis id（MAC 類型時另外正規化到 remote_mac）；CDP 是 Device ID
    remote_chassis_id: Mapped[str | None] = mapped_column(String(255))
    remote_mac: Mapped[str | None] = mapped_column(String(32))
    remote_mgmt_address: Mapped[str | None] = mapped_column(String(64))
    remote_platform: Mapped[str | None] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("protocol IN ('lldp','cdp')", name="snmp_neighbor_protocol"),
    )
