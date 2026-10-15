"""SNMP 收集 schemas（0217）。

機密（社群字串、v3 認證／加密密碼）只寫不讀：回應只有 has_*；更新時留空（None 或空字串）＝不變。
欄位之間的規則（v3 要帳號、authPriv 要加密密碼…）在 services/snmp.validate_credential 檢查，
錯誤回 ui_detail 代碼讓前端翻譯。
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.base import StrictModel

Version = Literal["v2c", "v3"]
SecurityLevel = Literal["authPriv", "authNoPriv"]
AuthProtocol = Literal["MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512"]
PrivProtocol = Literal["DES", "AES-128", "AES-192", "AES-256"]
Collector = Literal["auto", "agent", "server"]

_Secret = Annotated[str | None, Field(max_length=128)]


class SnmpCredentialBase(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    is_default: bool = False
    customer_id: uuid.UUID | None = None
    version: Version = "v2c"
    username: Annotated[str | None, Field(max_length=64)] = None
    security_level: SecurityLevel | None = None
    auth_protocol: AuthProtocol | None = None
    priv_protocol: PrivProtocol | None = None
    timeout_s: Annotated[int, Field(ge=1, le=30)] = 5
    retries: Annotated[int, Field(ge=0, le=5)] = 1
    description: Annotated[str | None, Field(max_length=2048)] = None


class SnmpCredentialCreate(SnmpCredentialBase):
    community: _Secret = None
    auth_passphrase: _Secret = None
    priv_passphrase: _Secret = None


class SnmpCredentialUpdate(StrictModel):
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    is_default: bool | None = None
    customer_id: uuid.UUID | None = None
    version: Version | None = None
    username: Annotated[str | None, Field(max_length=64)] = None
    security_level: SecurityLevel | None = None
    auth_protocol: AuthProtocol | None = None
    priv_protocol: PrivProtocol | None = None
    timeout_s: Annotated[int | None, Field(ge=1, le=30)] = None
    retries: Annotated[int | None, Field(ge=0, le=5)] = None
    description: Annotated[str | None, Field(max_length=2048)] = None
    #: 留空＝不變（存了不回傳）
    community: _Secret = None
    auth_passphrase: _Secret = None
    priv_passphrase: _Secret = None


class SnmpCredentialRead(StrictModel):
    id: uuid.UUID
    name: str
    is_default: bool
    customer_id: uuid.UUID | None = None
    customer_name: str | None = None
    version: str
    username: str | None = None
    security_level: str | None = None
    auth_protocol: str | None = None
    priv_protocol: str | None = None
    timeout_s: int
    retries: int
    description: str | None = None
    has_community: bool = False
    has_auth_passphrase: bool = False
    has_priv_passphrase: bool = False
    #: 使用中的收集目標數（刪除前先看）
    target_count: int = 0
    created_at: datetime
    updated_at: datetime


def _check_ip(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    return str(ipaddress.ip_address(v.strip()))


def _check_intervals(v: dict[str, Any] | None) -> dict[str, int] | None:
    if not v:
        return None
    from app.models.snmp import SECTIONS
    out: dict[str, int] = {}
    for k, val in v.items():
        if k not in SECTIONS:
            raise ValueError(f"unknown section: {k}")
        n = int(val)
        if not 60 <= n <= 86400:
            raise ValueError("interval must be 60..86400 seconds")
        out[k] = n
    return out or None


class SnmpTargetBase(StrictModel):
    port: Annotated[int, Field(ge=1, le=65535)] = 161
    collect_arp: bool = True
    collect_fdb: bool = True
    collect_neighbors: bool = True
    collect_interfaces: bool = True
    collector: Collector = "auto"
    agent_id: uuid.UUID | None = None
    enabled: bool = True
    auto_create_ips: bool = False
    #: 管理位址覆寫；空＝用裝置的主要 IP
    ip_override: str | None = None
    #: 各區段間隔（秒）覆寫：{"arp": 300, "fdb": 900, "neighbors": 3600, "interfaces": 3600}
    intervals: dict[str, int] | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("ip_override")
    @classmethod
    def _ip(cls, v: str | None) -> str | None:
        return _check_ip(v)

    @field_validator("intervals")
    @classmethod
    def _iv(cls, v: dict[str, Any] | None) -> dict[str, int] | None:
        return _check_intervals(v)


class SnmpTargetCreate(SnmpTargetBase):
    #: 收集目標＝這台裝置。也可以改給 ip_address_id（子網路頁「對閘道啟用 SNMP 收集」）：
    #: 那筆 IP 已連結裝置就用那台；沒有就以它建一台裝置（類型 router、主要 IP＝它），二擇一
    device_id: uuid.UUID | None = None
    ip_address_id: uuid.UUID | None = None
    #: 空＝用預設憑證
    credential_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_of(self) -> SnmpTargetCreate:
        if (self.device_id is None) == (self.ip_address_id is None):
            raise ValueError("give exactly one of device_id or ip_address_id")
        return self


class SnmpTargetUpdate(StrictModel):
    credential_id: uuid.UUID | None = None
    port: Annotated[int | None, Field(ge=1, le=65535)] = None
    collect_arp: bool | None = None
    collect_fdb: bool | None = None
    collect_neighbors: bool | None = None
    collect_interfaces: bool | None = None
    collector: Collector | None = None
    agent_id: uuid.UUID | None = None
    enabled: bool | None = None
    auto_create_ips: bool | None = None
    ip_override: str | None = None
    intervals: dict[str, int] | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("ip_override")
    @classmethod
    def _ip(cls, v: str | None) -> str | None:
        return _check_ip(v)

    @field_validator("intervals")
    @classmethod
    def _iv(cls, v: dict[str, Any] | None) -> dict[str, int] | None:
        return _check_intervals(v)


class SnmpTargetBulk(StrictModel):
    """裝置清單的批次指派：沒有收集目標的裝置新建、已有的改憑證（以及有給的其他欄位）。"""
    device_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=1000)]
    #: 空＝用預設憑證
    credential_id: uuid.UUID | None = None
    collect_arp: bool | None = None
    collect_fdb: bool | None = None
    collect_neighbors: bool | None = None
    collect_interfaces: bool | None = None
    collector: Collector | None = None
    agent_id: uuid.UUID | None = None
    enabled: bool | None = None


class SnmpTestIn(StrictModel):
    #: 要試的憑證（最多 5 組）；空＝用目標設定的那一組
    credential_ids: Annotated[list[uuid.UUID] | None, Field(max_length=5)] = None
