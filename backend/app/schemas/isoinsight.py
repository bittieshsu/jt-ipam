"""ISOinsight 整合的 schemas。讀取用的 schema 不帶密碼（只有 has_password），Cookie／Token 從不存也從不回。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.schemas.base import StrictModel
from app.services.isoinsight import config as cfg

Method = Literal["GET", "POST"]
PostFormat = Literal["form", "json"]
AuthMode = Literal["cookie", "token"]
#: 帳密原樣保存：前後空白也是帳密的一部分（模型預設會去掉字串前後空白）
_Exact = StringConstraints(strip_whitespace=False)


class _Validators(StrictModel):
    @field_validator("base_url", check_fields=False)
    @classmethod
    def _base(cls, v: str | None) -> str | None:
        return None if v is None else cfg.normalize_base_url(v)

    @field_validator("login_path", check_fields=False)
    @classmethod
    def _login(cls, v: str | None) -> str | None:
        return None if v is None else cfg.normalize_path(v, allow_query=False)

    @field_validator("lease_path", check_fields=False)
    @classmethod
    def _lease(cls, v: str | None) -> str | None:
        return None if v is None else cfg.normalize_path(v, allow_query=True)

    @field_validator("username_param", "password_param", check_fields=False)
    @classmethod
    def _param(cls, v: str | None) -> str | None:
        return None if v is None else cfg.check_param_name(v)

    @field_validator("token_path", check_fields=False)
    @classmethod
    def _tpath(cls, v: str | None) -> str | None:
        return None if v in (None, "") else cfg.check_token_path(v)

    @field_validator("token_header", check_fields=False)
    @classmethod
    def _theader(cls, v: str | None) -> str | None:
        return None if v is None else cfg.check_header_name(v)

    @field_validator("token_prefix", check_fields=False)
    @classmethod
    def _tprefix(cls, v: str | None) -> str | None:
        return None if v is None else cfg.check_token_prefix(v)

    @field_validator("source_timezone", check_fields=False)
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else cfg.check_timezone(v)


class IsoInsightCreate(_Validators):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    base_url: Annotated[str, Field(min_length=8, max_length=2048)]
    product_version: Annotated[str | None, Field(max_length=128)] = None
    login_path: Annotated[str, Field(max_length=255)] = cfg.DEFAULT_LOGIN_PATH
    login_method: Method = "POST"
    post_format: PostFormat = "form"
    username: Annotated[str, _Exact, Field(min_length=1, max_length=255)]
    password: Annotated[str, _Exact, Field(min_length=1, max_length=512)]
    username_param: Annotated[str, Field(max_length=64)] = "username"
    password_param: Annotated[str, Field(max_length=64)] = "password"  # noqa: S105 -- 參數名／標頭名，不是秘密
    auth_mode: AuthMode = "cookie"
    token_path: Annotated[str | None, Field(max_length=255)] = None
    token_header: Annotated[str, Field(max_length=64)] = "Authorization"  # noqa: S105 -- 參數名／標頭名，不是秘密
    token_prefix: Annotated[str, Field(max_length=32)] = "Bearer"  # noqa: S105 -- 參數名／標頭名，不是秘密
    lease_path: Annotated[str, Field(max_length=255)] = cfg.DEFAULT_LEASE_PATH
    verify_tls: bool = True
    source_timezone: Annotated[str, Field(max_length=64)] = cfg.DEFAULT_TIMEZONE
    # 規格：來源時區「儲存前由管理員確認」—— 沒有勾就不收（伺服器自己的時區不能默默代替）
    timezone_confirmed: bool = False
    customer_id: uuid.UUID | None = None
    scope_subnet_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=5000)]
    create_ips: bool = True
    enabled: bool = True
    schedule_enabled: bool = True
    sync_interval_seconds: Annotated[int, Field(ge=60, le=86400)] = 300
    connect_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 5
    request_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30
    job_timeout_seconds: Annotated[int, Field(ge=10, le=900)] = 120
    max_response_mib: Annotated[int, Field(ge=1, le=200)] = 20
    max_rows: Annotated[int, Field(ge=1, le=1_000_000)] = 100_000
    description: Annotated[str | None, Field(max_length=2048)] = None

    @model_validator(mode="after")
    def _token(self) -> IsoInsightCreate:
        if self.auth_mode == "token" and not self.token_path:
            raise ValueError("token_path is required in token mode")
        return self


class IsoInsightUpdate(_Validators):
    """全部選用；`password` 留空＝保留原密碼（要更換才填）。"""
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    base_url: Annotated[str | None, Field(min_length=8, max_length=2048)] = None
    product_version: Annotated[str | None, Field(max_length=128)] = None
    login_path: Annotated[str | None, Field(max_length=255)] = None
    login_method: Method | None = None
    post_format: PostFormat | None = None
    username: Annotated[str | None, _Exact, Field(min_length=1, max_length=255)] = None
    password: Annotated[str | None, _Exact, Field(max_length=512)] = None
    username_param: Annotated[str | None, Field(max_length=64)] = None
    password_param: Annotated[str | None, Field(max_length=64)] = None
    auth_mode: AuthMode | None = None
    token_path: Annotated[str | None, Field(max_length=255)] = None
    token_header: Annotated[str | None, Field(max_length=64)] = None
    token_prefix: Annotated[str | None, Field(max_length=32)] = None
    lease_path: Annotated[str | None, Field(max_length=255)] = None
    verify_tls: bool | None = None
    source_timezone: Annotated[str | None, Field(max_length=64)] = None
    timezone_confirmed: bool | None = None
    customer_id: uuid.UUID | None = None
    scope_subnet_ids: Annotated[list[uuid.UUID] | None, Field(max_length=5000)] = None
    create_ips: bool | None = None
    enabled: bool | None = None
    schedule_enabled: bool | None = None
    sync_interval_seconds: Annotated[int | None, Field(ge=60, le=86400)] = None
    connect_timeout_seconds: Annotated[int | None, Field(ge=1, le=60)] = None
    request_timeout_seconds: Annotated[int | None, Field(ge=1, le=300)] = None
    job_timeout_seconds: Annotated[int | None, Field(ge=10, le=900)] = None
    max_response_mib: Annotated[int | None, Field(ge=1, le=200)] = None
    max_rows: Annotated[int | None, Field(ge=1, le=1_000_000)] = None
    description: Annotated[str | None, Field(max_length=2048)] = None


class IsoInsightRead(StrictModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    base_url: str
    product_version: str | None = None
    login_path: str
    login_method: str
    post_format: str
    username: str
    has_password: bool = False
    username_param: str
    password_param: str
    auth_mode: str
    token_path: str | None = None
    token_header: str
    token_prefix: str
    lease_path: str
    verify_tls: bool
    source_timezone: str
    customer_id: uuid.UUID | None = None
    scope_subnet_ids: list[uuid.UUID] = []
    create_ips: bool
    enabled: bool
    schedule_enabled: bool
    sync_interval_seconds: int
    connect_timeout_seconds: int
    request_timeout_seconds: int
    job_timeout_seconds: int
    max_response_mib: int
    max_rows: int
    description: str | None = None
    config_version: int
    last_test_ok_at: datetime | None = None
    preview_ok_at: datetime | None = None
    auth_hold: bool
    retry_after_until: datetime | None = None
    last_attempt_at: datetime | None = None
    last_fetch_ok_at: datetime | None = None
    last_commit_at: datetime | None = None
    last_full_success_at: datetime | None = None
    last_result: str | None = None
    last_error_code: str | None = None
    last_error: str | None = None
    last_summary: dict[str, Any] | None = None
    running: bool = False
    # 執行位置：目前只有 jt-ipam 伺服器（沒有「經代理執行 HTTP 整合」的既有機制）
    run_on: str = "server"
    created_at: datetime
    updated_at: datetime


class IsoInsightRunRead(StrictModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    kind: str
    trigger: str
    actor_user_id: uuid.UUID | None = None
    background_task_id: uuid.UUID | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    result: str
    stage: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    login_method: str | None = None
    auth_mode: str | None = None
    fetched: int
    valid: int
    invalid: int
    duplicates: int
    created: int
    updated: int
    unchanged: int
    observed_only: int
    expired: int
    unknown_time: int
    unmatched: int
    conflicts: int
    quality: dict[str, Any] | None = None
    warnings: list[str] | None = None
    stages: list[Any] | None = None


class IsoInsightLeaseRead(StrictModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    source_id: uuid.UUID
    source_name: str | None = None
    ip: str
    mac: str | None = None
    name: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    start_raw: str | None = None
    end_raw: str | None = None
    # 依時間推定（查詢當下）：active／expired／not_started／invalid_period／unknown —— 不是上線狀態
    state: str
    quality: list[str] = []
    raw_count: int
    match_status: str
    subnet_id: uuid.UUID | None = None
    subnet_cidr: str | None = None
    ip_address_id: uuid.UUID | None = None
    first_observed_at: datetime
    lease_observed_at: datetime


class IsoInsightTestRequest(StrictModel):
    # 人工觸發的額外診斷：不登入直接讀租約（不列入排程）
    anonymous_check: bool = False
