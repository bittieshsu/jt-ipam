"""Check Point 第二階段（閘道的 Gaia API）的 schemas。讀取用的不帶密碼（只有 has_secret）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, field_validator

from app.schemas.base import StrictModel
from app.schemas.dhcp_standalone import _http_url


class GaiaTargetCreate(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    gaia_url: Annotated[str, Field(min_length=8, max_length=2048)]
    username: Annotated[str, Field(min_length=1, max_length=255)]
    secret: Annotated[str, Field(min_length=1, max_length=1024)]
    gateway_uid: Annotated[str | None, Field(max_length=64)] = None
    domain: Annotated[str, Field(max_length=128)] = ""
    verify_tls: bool = True
    enabled: bool = True
    sync_interval_seconds: Annotated[int, Field(ge=60, le=86400)] = 300
    sync_dhcp: bool = True
    allow_scripts: bool = True
    sync_arp: bool = True
    sync_leases: bool = True
    scope_subnet_ids: list[uuid.UUID] | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("gaia_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return _http_url(v) or v


class GaiaTargetUpdate(StrictModel):
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    gaia_url: Annotated[str | None, Field(min_length=8, max_length=2048)] = None
    username: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    secret: Annotated[str | None, Field(max_length=1024)] = None       # 留空＝不更改
    verify_tls: bool | None = None
    enabled: bool | None = None
    sync_interval_seconds: Annotated[int | None, Field(ge=60, le=86400)] = None
    sync_dhcp: bool | None = None
    allow_scripts: bool | None = None
    sync_arp: bool | None = None
    sync_leases: bool | None = None
    scope_subnet_ids: list[uuid.UUID] | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("gaia_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        return _http_url(v)


class GaiaTargetRead(StrictModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    server_id: uuid.UUID
    domain: str
    gateway_uid: str | None = None
    name: str
    gaia_url: str
    username: str
    has_secret: bool = False
    verify_tls: bool
    enabled: bool
    sync_interval_seconds: int
    sync_dhcp: bool
    allow_scripts: bool
    sync_arp: bool
    sync_leases: bool
    scope_subnet_ids: list[uuid.UUID] | None = None
    api_version: str | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None
    last_summary: dict[str, Any] | None = None
    description: str | None = None
    created_at: datetime
    updated_at: datetime
