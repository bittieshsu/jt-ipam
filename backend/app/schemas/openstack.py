"""CubeCOS／OpenStack 整合的 schema。密碼（secret）與 CA 憑證（ca_cert）只寫不讀。"""

from __future__ import annotations

import ssl
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.base import StrictModel

_Name = Annotated[str, Field(min_length=1, max_length=128)]
_Opt255 = Annotated[str | None, Field(max_length=255)]


def _check_url(v: str | None, *, required: bool) -> str | None:
    from urllib.parse import urlsplit
    if v is None or not v.strip():
        if required:
            raise ValueError("URL is required")
        return None
    v = v.strip()
    sp = urlsplit(v)
    if sp.scheme not in ("http", "https") or not sp.hostname:
        raise ValueError("must be an http(s) URL with a host")
    if sp.username or sp.password or sp.query or sp.fragment:
        raise ValueError("URL must not contain credentials, a query or a fragment")
    return v


def _check_pem(v: str | None) -> str | None:
    """""＝清掉；None＝不改（PATCH）。內容要讀得成 CA 憑證（讀不了的話連線時才失敗，使用者找不到原因）。"""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return ""
    if "-----BEGIN CERTIFICATE-----" not in v:
        raise ValueError("ca_cert must be a PEM certificate")
    try:
        ssl.create_default_context(cadata=v)
    except (ssl.SSLError, ValueError) as exc:
        raise ValueError(f"ca_cert is not a valid PEM certificate: {exc}") from exc
    return v + "\n"


class OpenStackBase(StrictModel):
    name: _Name
    product: Literal["openstack", "cubecos"] = "openstack"
    auth_url: Annotated[str, Field(min_length=1, max_length=2048)]
    auth_method: Literal["password", "app_credential"] = "password"
    username: _Opt255 = None
    user_domain: Annotated[str, Field(min_length=1, max_length=255)] = "Default"
    project_name: _Opt255 = None
    project_domain: Annotated[str, Field(min_length=1, max_length=255)] = "Default"
    app_credential_id: _Opt255 = None
    region: _Opt255 = None
    interface: Literal["public", "internal", "admin"] = "public"
    verify_tls: bool = True
    all_projects: bool = True
    cubecos_url: Annotated[str | None, Field(max_length=2048)] = None
    enabled: bool = True
    sync_interval_seconds: Annotated[int, Field(ge=60, le=86400)] = 300
    scope_subnet_ids: list[uuid.UUID] | None = None
    auto_create_ips: bool = False
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("auth_url")
    @classmethod
    def _v_auth_url(cls, v: str) -> str:
        out = _check_url(v, required=True)
        assert out is not None
        return out

    @field_validator("cubecos_url")
    @classmethod
    def _v_cubecos(cls, v: str | None) -> str | None:
        return _check_url(v, required=False)

    @field_validator("region", "username", "project_name", "app_credential_id")
    @classmethod
    def _v_blank(cls, v: str | None) -> str | None:
        return v.strip() or None if isinstance(v, str) else v


def check_auth_fields(auth_method: str, username: str | None, project_name: str | None,
                      app_credential_id: str | None) -> None:
    """密碼方式要帳號與 project；Application Credential 要 id。"""
    if auth_method == "password" and not (username and project_name):
        raise ValueError("password authentication needs username and project_name")
    if auth_method == "app_credential" and not app_credential_id:
        raise ValueError("application credential authentication needs app_credential_id")


class OpenStackCreate(OpenStackBase):
    secret: Annotated[str, Field(min_length=1, max_length=1024)]
    ca_cert: Annotated[str | None, Field(max_length=65536)] = None

    @field_validator("ca_cert")
    @classmethod
    def _v_ca(cls, v: str | None) -> str | None:
        return _check_pem(v)

    @model_validator(mode="after")
    def _v_auth(self) -> OpenStackCreate:
        check_auth_fields(self.auth_method, self.username, self.project_name, self.app_credential_id)
        return self


class OpenStackUpdate(StrictModel):
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    product: Literal["openstack", "cubecos"] | None = None
    auth_url: Annotated[str | None, Field(min_length=1, max_length=2048)] = None
    auth_method: Literal["password", "app_credential"] | None = None
    username: _Opt255 = None
    user_domain: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    project_name: _Opt255 = None
    project_domain: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    app_credential_id: _Opt255 = None
    # 不給＝保留原本的
    secret: Annotated[str | None, Field(min_length=1, max_length=1024)] = None
    region: _Opt255 = None
    interface: Literal["public", "internal", "admin"] | None = None
    verify_tls: bool | None = None
    # 不給＝保留；""＝清掉
    ca_cert: Annotated[str | None, Field(max_length=65536)] = None
    all_projects: bool | None = None
    cubecos_url: Annotated[str | None, Field(max_length=2048)] = None
    enabled: bool | None = None
    sync_interval_seconds: Annotated[int | None, Field(ge=60, le=86400)] = None
    scope_subnet_ids: list[uuid.UUID] | None = None
    auto_create_ips: bool | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("auth_url")
    @classmethod
    def _v_auth_url(cls, v: str | None) -> str | None:
        return _check_url(v, required=True) if v is not None else None

    @field_validator("cubecos_url")
    @classmethod
    def _v_cubecos(cls, v: str | None) -> str | None:
        return _check_url(v, required=False)

    @field_validator("ca_cert")
    @classmethod
    def _v_ca(cls, v: str | None) -> str | None:
        return _check_pem(v)

    @field_validator("region", "username", "project_name", "app_credential_id")
    @classmethod
    def _v_blank(cls, v: str | None) -> str | None:
        return v.strip() or None if isinstance(v, str) else v


class OpenStackRead(StrictModel):
    id: uuid.UUID
    name: str
    product: str
    auth_url: str
    auth_method: str
    username: str | None = None
    user_domain: str
    project_name: str | None = None
    project_domain: str
    app_credential_id: str | None = None
    has_secret: bool
    region: str | None = None
    interface: str
    verify_tls: bool
    has_ca_cert: bool
    all_projects: bool
    cubecos_url: str | None = None
    enabled: bool
    cluster_id: uuid.UUID | None = None
    sync_interval_seconds: int
    last_sync_at: datetime | None = None
    last_error: str | None = None
    last_full_success_at: datetime | None = None
    scope_subnet_ids: list[str] | None = None
    auto_create_ips: bool
    description: str | None = None
    capabilities: dict[str, Any] | None = None
    vm_count: int = 0
    port_count: int = 0
    node_count: int = 0
    created_at: datetime


class OpenStackTestResult(StrictModel):
    ok: bool
    capabilities: dict[str, Any]
    error: dict[str, Any] | None = None


class OpenStackSubnetRead(StrictModel):
    id: uuid.UUID
    subnet_id: str
    name: str | None = None
    network_id: str | None = None
    network_name: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    cidr: str
    ip_version: int | None = None
    gateway_ip: str | None = None
    allocation_pools: list[Any] | None = None
    enable_dhcp: bool | None = None
    shared: bool | None = None
    mapped_subnet_id: uuid.UUID | None = None
    mapped_subnet_cidr: str | None = None
    mapped_subnet_name: str | None = None
    mapped_manually: bool
    pending: bool
    missing_syncs: int
    first_seen_at: datetime
    last_seen_at: datetime


class OpenStackSubnetMapping(StrictModel):
    # null＝手動設成「不對應」（同樣不會被同步覆蓋）
    mapped_subnet_id: uuid.UUID | None


class OpenStackPortRead(StrictModel):
    id: uuid.UUID
    instance_id: uuid.UUID
    instance_name: str
    product: str
    port_id: str
    mac: str | None = None
    status: str | None = None
    device_owner: str | None = None
    network_id: str | None = None
    network_name: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    fixed_ips: list[Any] = []
    floating_ips: list[Any] = []
    binding_host: str | None = None
    vm_id: uuid.UUID | None = None
    vm_name: str | None = None
    vm_external_id: str | None = None
    vm_status: str | None = None
    vm_node: str | None = None
    last_seen_at: datetime
    instance_last_sync_at: datetime | None = None
