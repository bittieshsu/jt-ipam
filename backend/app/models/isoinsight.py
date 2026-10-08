"""ISOinsight 整合（2026-10-07）：來源設定、來源租約觀察、同步記錄。

- `isoinsight_sources`：一台 ISOinsight 一列。密碼 AES-GCM 加密；Cookie／Token 不存（每次工作現登入）
- `isoinsight_leases`：來源回報的租約（逐來源、逐 IP、逐 MAC 一列）。**來源觀察**，不是正式 IP：
  IP／MAC／主機名稱怎麼套用到 IP 記錄，由 services/isoinsight/reconcile.py 依既有優先序決定
- `isoinsight_sync_runs`：每一次測試、預覽、同步的記錄（計數是可查詢的欄位，不塞成一包 JSON）

⚠️ 不依「這次沒出現」推定租約釋放：全量語意還沒有在真機確認，只用租約到期時間與最後觀察時間管理。
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class IsoInsightSource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "isoinsight_sources"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # https://192.0.2.10[/前綴]：不含帳密、Query、片段；登入與租約路徑都是它底下的相對路徑
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    product_version: Mapped[str | None] = mapped_column(String(128))   # 客戶手動填的產品版本備註

    login_path: Mapped[str] = mapped_column(String(255), nullable=False, server_default="/api/logon")
    # 明確設定、沒有自動 fallback（避免重複送帳密觸發帳號鎖定）
    login_method: Mapped[str] = mapped_column(String(8), nullable=False, server_default="POST")
    post_format: Mapped[str] = mapped_column(String(8), nullable=False, server_default="form")
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    password_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    username_param: Mapped[str] = mapped_column(String(64), nullable=False, server_default="username")
    password_param: Mapped[str] = mapped_column(String(64), nullable=False, server_default="password")

    auth_mode: Mapped[str] = mapped_column(String(8), nullable=False, server_default="cookie")
    # Token 模式才用；欄位路徑與 Header 由管理員明確設定（不是原廠已確認的欄位）
    token_path: Mapped[str | None] = mapped_column(String(255))
    token_header: Mapped[str] = mapped_column(String(64), nullable=False, server_default="Authorization")
    token_prefix: Mapped[str] = mapped_column(String(32), nullable=False, server_default="Bearer")

    lease_path: Mapped[str] = mapped_column(String(255), nullable=False, server_default="/isosvc?act=DhcpLease")
    verify_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    source_timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="Asia/Taipei")

    # 隔離範圍：租戶（客戶）。允許的子網路必須都屬於它（NULL＝不屬於任何客戶的子網路）
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), index=True)
    # 允許同步的子網路（必選）：只在這些子網路內配對，最長首碼；重疊而無法唯一判定就不配對
    scope_subnet_ids: Mapped[list[Any]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]"))
    # 唯一配對到允許子網路、而且在租約期間內 → 可以新增正式 IP（關掉就只更新既有的）
    create_ips: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # 預設開啟（使用者 2026-10-08）；還沒用目前的設定成功預覽過時，排程輪到也只記「要先預覽」不同步
    # （連線設定改了要重新預覽才會再同步）
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    connect_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    job_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="120")
    max_response_mib: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    max_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100000")
    description: Mapped[str | None] = mapped_column(Text)

    # 設定版本：每次改設定 +1；進行中的工作提交前重新確認，對不上就不提交
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # 連線設定改過之後要重新測試／預覽，不沿用舊的成功標記
    last_test_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preview_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 登入失敗後暫停排程（不每五分鐘送一次錯的帳密去觸發帳號鎖定）；改設定或手動成功一次就解除
    auth_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # 429 的 Retry-After：排程在這之前不再嘗試
    retry_after_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 四個時間分開記，部分成功或失敗不會被顯示成完全正常
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fetch_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_commit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(16))
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    # 健康告警與畫面看的錯誤原文（已遮蔽）；部分成功時是 NULL（那不是同步失敗）
    last_error: Mapped[str | None] = mapped_column(Text)
    last_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # 工作鎖（手動與排程共用；跨行程）：取得時寫 token＋時間，逾時視為殘留
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    running_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint("login_method IN ('GET','POST')", name="isoinsight_login_method"),
        CheckConstraint("post_format IN ('form','json')", name="isoinsight_post_format"),
        CheckConstraint("auth_mode IN ('cookie','token')", name="isoinsight_auth_mode"),
        CheckConstraint("auth_mode <> 'token' OR token_path IS NOT NULL", name="isoinsight_token_path"),
        CheckConstraint("sync_interval_seconds >= 60", name="isoinsight_interval_min"),
    )


class IsoInsightLease(Base, UUIDPrimaryKeyMixin):
    """來源回報的一筆租約（逐來源、逐 IP、逐 MAC）。同 IP 不同 MAC 各留一列（各自的證據）。"""

    __tablename__ = "isoinsight_leases"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("isoinsight_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    ip: Mapped[str] = mapped_column(INET, nullable=False, index=True)
    # 正規化的 MAC（無分隔小寫）；空白或不合法＝''（唯一鍵的一部分，所以不用 NULL）
    mac_key: Mapped[str] = mapped_column(String(12), nullable=False, server_default="")
    mac: Mapped[str | None] = mapped_column(String(17), index=True)
    # 來源主機名稱（攻擊者可控的文字：只當資料，顯示時轉義）。空白不蓋掉上次看到的名稱
    name: Mapped[str | None] = mapped_column(String(255))
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 時間解析不了時留原文（截短）給人看；不自行解讀成「永久」
    start_raw: Mapped[str | None] = mapped_column(String(40))
    end_raw: Mapped[str | None] = mapped_column(String(40))
    # 品質標籤（可重疊）：mac_empty／mac_invalid／name_empty／time_unknown／not_started／invalid_period／
    # mac_conflict／duplicate／ipv6_unverified
    quality: Mapped[list[str]] = mapped_column(
        ARRAY(String(24)), nullable=False, server_default=text("'{}'::varchar[]"))
    raw_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    # 子網路配對結果：matched（唯一配對）／ambiguous（重疊、無法唯一判定）／no_subnet（不在允許範圍）
    match_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="no_subnet")
    subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subnets.id", ondelete="SET NULL"), index=True)
    ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_addresses.id", ondelete="SET NULL"), index=True)

    # 觀察時間（jt-ipam 取得資料的 UTC 時間），與租約本身的開始／到期時間分開
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "ip", "mac_key", name="uq_isoinsight_lease"),
        Index("ix_isoinsight_leases_source_observed", "source_id", "lease_observed_at"),
        Index("ix_isoinsight_leases_end_at", "end_at"),
        CheckConstraint("match_status IN ('matched','ambiguous','no_subnet')", name="isoinsight_match_status"),
    )


class IsoInsightSyncRun(Base, UUIDPrimaryKeyMixin):
    """一次測試／預覽／同步的記錄。處理結果計數彼此互斥（加總＝去重後的租約數），品質標籤另計可重疊。"""

    __tablename__ = "isoinsight_sync_runs"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("isoinsight_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)          # sync／test／preview
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)       # manual／scheduled
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True)
    background_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    config_version: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # running／success／partial／failed／skipped
    result: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    stage: Mapped[str | None] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_detail: Mapped[str | None] = mapped_column(Text)       # 已遮蔽
    login_method: Mapped[str | None] = mapped_column(String(8))
    auth_mode: Mapped[str | None] = mapped_column(String(8))

    fetched: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    valid: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    invalid: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    observed_only: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    expired: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unknown_time: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unmatched: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    conflicts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 品質標籤計數（標籤 → 筆數；可重疊）與相容性警告；測試連線的逐步記錄（已遮蔽）
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[str] | None] = mapped_column(ARRAY(String(48)))
    stages: Mapped[list[Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_isoinsight_sync_runs_source_started", "source_id", "started_at"),
        CheckConstraint("kind IN ('sync','test','preview')", name="isoinsight_run_kind"),
        CheckConstraint("trigger IN ('manual','scheduled')", name="isoinsight_run_trigger"),
        CheckConstraint("result IN ('running','success','partial','failed','skipped')",
                        name="isoinsight_run_result"),
    )
