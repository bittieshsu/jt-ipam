"""Technitium DHCP 整合的 schemas。讀取用的不帶 token（只有 has_token）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, field_validator

from app.schemas.base import StrictModel
from app.schemas.dhcp_standalone import _http_url


class TechnitiumDhcpBase(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    api_url: Annotated[str, Field(min_length=8, max_length=2048)]
    verify_tls: bool = True
    enabled: bool = True
    sync_scopes: bool = True
    sync_leases: bool = True
    sync_interval_seconds: Annotated[int, Field(ge=60, le=86400)] = 300
    description: Annotated[str | None, Field(max_length=2048)] = None
    scope_subnet_ids: list[uuid.UUID] | None = None

    @field_validator("api_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return _http_url(v) or v


class TechnitiumDhcpCreate(TechnitiumDhcpBase):
    # Technitium 的 API token（使用者選單「建立 API token」，或管理 → 工作階段）
    token: Annotated[str, Field(min_length=8, max_length=512)]


class TechnitiumDhcpUpdate(StrictModel):
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    api_url: Annotated[str | None, Field(min_length=8, max_length=2048)] = None
    verify_tls: bool | None = None
    token: Annotated[str | None, Field(max_length=512)] = None     # 留空＝不更改
    enabled: bool | None = None
    sync_scopes: bool | None = None
    sync_leases: bool | None = None
    sync_interval_seconds: Annotated[int | None, Field(ge=60, le=86400)] = None
    description: Annotated[str | None, Field(max_length=2048)] = None
    scope_subnet_ids: list[uuid.UUID] | None = None

    @field_validator("api_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        return _http_url(v)


class TechnitiumDhcpRead(StrictModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    api_url: str
    verify_tls: bool
    has_token: bool = False
    enabled: bool
    sync_scopes: bool
    sync_leases: bool
    sync_interval_seconds: int
    description: str | None = None
    scope_subnet_ids: list[uuid.UUID] | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None
    last_summary: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class TechnitiumScopeRead(StrictModel):
    name: str
    enabled: bool
    subnet_cidr: str | None = None
    start_ip: str
    end_ip: str
    exclusions: list[dict[str, str]] = Field(default_factory=list)
    router: str | None = None
    dns_servers: list[str] = Field(default_factory=list)
    ntp_servers: list[str] = Field(default_factory=list)
    wins_servers: list[str] = Field(default_factory=list)
    domain_name: str | None = None
    lease_seconds: int | None = None
    reservations: int = 0
    synced_at: datetime | None = None
