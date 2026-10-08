"""Check Point 整合的 schemas。讀取用的不帶 API key／密碼（只有 has_secret）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from app.schemas.base import StrictModel
from app.schemas.dhcp_standalone import _http_url

AuthMode = Literal["api_key", "password"]


def _clean_list(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    out = [x.strip() for x in v if x and x.strip()]
    return out[:200]


class CheckPointBase(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    api_url: Annotated[str, Field(min_length=8, max_length=2048)]
    verify_tls: bool = True
    auth_mode: AuthMode = "api_key"
    username: Annotated[str | None, Field(max_length=255)] = None
    domains: list[Annotated[str, Field(max_length=128)]] | None = None
    packages: list[Annotated[str, Field(max_length=128)]] | None = None
    enabled: bool = True
    sync_interval_seconds: Annotated[int, Field(ge=300, le=86400)] = 900
    sync_objects: bool = True
    sync_policies: bool = True
    sync_nat: bool = True
    scope_subnet_ids: list[uuid.UUID] | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("api_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return _http_url(v) or v

    @field_validator("domains", "packages")
    @classmethod
    def _lists(cls, v: list[str] | None) -> list[str] | None:
        return _clean_list(v)


class CheckPointCreate(CheckPointBase):
    # API key（建議）或密碼
    secret: Annotated[str, Field(min_length=4, max_length=1024)]


class CheckPointUpdate(StrictModel):
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    api_url: Annotated[str | None, Field(min_length=8, max_length=2048)] = None
    verify_tls: bool | None = None
    auth_mode: AuthMode | None = None
    username: Annotated[str | None, Field(max_length=255)] = None
    secret: Annotated[str | None, Field(max_length=1024)] = None     # 留空＝不更改
    domains: list[Annotated[str, Field(max_length=128)]] | None = None
    packages: list[Annotated[str, Field(max_length=128)]] | None = None
    enabled: bool | None = None
    sync_interval_seconds: Annotated[int | None, Field(ge=300, le=86400)] = None
    sync_objects: bool | None = None
    sync_policies: bool | None = None
    sync_nat: bool | None = None
    scope_subnet_ids: list[uuid.UUID] | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("api_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        return _http_url(v)

    @field_validator("domains", "packages")
    @classmethod
    def _lists(cls, v: list[str] | None) -> list[str] | None:
        return _clean_list(v)


class CheckPointRead(StrictModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    api_url: str
    verify_tls: bool
    auth_mode: str
    username: str | None = None
    has_secret: bool = False
    domains: list[str] | None = None
    packages: list[str] | None = None
    enabled: bool
    sync_interval_seconds: int
    sync_objects: bool
    sync_policies: bool
    sync_nat: bool
    scope_subnet_ids: list[uuid.UUID] | None = None
    api_version: str | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None
    last_summary: dict[str, Any] | None = None
    description: str | None = None
    created_at: datetime
    updated_at: datetime
