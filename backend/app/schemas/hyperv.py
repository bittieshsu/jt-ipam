"""Hyper-V 整合的 schema。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.base import StrictModel

_HOST = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


def _check_host(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.strip()
    if not _HOST.match(v):
        raise ValueError("host must be a host name or IP address (no scheme, no path)")
    return v


def _check_hosts(v: str | None) -> str | None:
    if not v:
        return None
    parts = [p for p in re.split(r"[\s,]+", v) if p]
    for p in parts:
        _check_host(p)
    return "\n".join(parts) or None


def _kerberos_hosts(auth: str | None, *hosts: str | None) -> None:
    """Kerberos 的服務票是 HTTP/<主機名稱>：主機一律要 FQDN（IP 位址拿不到票）。"""
    if auth != "kerberos":
        return
    from app.services.winrm_kerberos import KerberosError, require_fqdn
    for h in hosts:
        for tok in re.split(r"[\s,]+", h or ""):
            if tok:
                try:
                    require_fqdn(tok)
                except KerberosError as exc:
                    raise ValueError(str(exc)) from exc


class HyperVBase(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    # hosts：逐台直連 Hyper-V 主機；scvmm：連 System Center VMM 伺服器
    mode: Literal["hosts", "scvmm"] = "hosts"
    host: Annotated[str, Field(min_length=1, max_length=255)]
    # hosts：同一個叢集的其他節點（每台都會連）；scvmm：備援 VMM 伺服器。**前端一律會送這個鍵**（StrictModel）
    extra_hosts: Annotated[str | None, Field(max_length=4096)] = None
    port: Annotated[int, Field(ge=1, le=65535)] = 5985
    use_ssl: bool = False
    verify_tls: bool = True
    username: Annotated[str, Field(min_length=1, max_length=255)]
    # WinRM 驗證（0223）：ntlm 或 kerberos。Kerberos：帳號寫 user@domain（或填領域），主機要 FQDN
    auth_method: Literal["ntlm", "kerberos"] = "ntlm"
    kerberos_realm: Annotated[str | None, Field(max_length=255, pattern=r"^[A-Za-z0-9.-]*$")] = None
    # 留空＝查 DNS SRV；填了就用填的（換行或逗號分隔）
    kerberos_kdc: Annotated[str | None, Field(max_length=1024, pattern=r"^[A-Za-z0-9.:\[\]\s,-]*$")] = None
    enabled: bool = True
    sync_interval_seconds: Annotated[int, Field(ge=60, le=86400)] = 600
    scope_subnet_ids: list[str] | None = None
    auto_create_ips: bool | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("host")
    @classmethod
    def _v_host(cls, v: str | None) -> str | None:
        return _check_host(v)

    @field_validator("extra_hosts")
    @classmethod
    def _v_hosts(cls, v: str | None) -> str | None:
        return _check_hosts(v)

    @model_validator(mode="after")
    def _v_kerberos(self) -> HyperVBase:
        _kerberos_hosts(self.auth_method, self.host, self.extra_hosts)
        return self


class HyperVCreate(HyperVBase):
    password: Annotated[str, Field(min_length=1, max_length=512)]


class HyperVUpdate(StrictModel):
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    mode: Literal["hosts", "scvmm"] | None = None
    host: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    extra_hosts: Annotated[str | None, Field(max_length=4096)] = None
    port: Annotated[int | None, Field(ge=1, le=65535)] = None
    use_ssl: bool | None = None
    verify_tls: bool | None = None
    username: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    password: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    auth_method: Literal["ntlm", "kerberos"] | None = None
    kerberos_realm: Annotated[str | None, Field(max_length=255, pattern=r"^[A-Za-z0-9.-]*$")] = None
    kerberos_kdc: Annotated[str | None, Field(max_length=1024, pattern=r"^[A-Za-z0-9.:\[\]\s,-]*$")] = None
    enabled: bool | None = None
    sync_interval_seconds: Annotated[int | None, Field(ge=60, le=86400)] = None
    scope_subnet_ids: list[str] | None = None
    auto_create_ips: bool | None = None
    description: Annotated[str | None, Field(max_length=2048)] = None

    @field_validator("host")
    @classmethod
    def _v_host(cls, v: str | None) -> str | None:
        return _check_host(v)

    @field_validator("extra_hosts")
    @classmethod
    def _v_hosts(cls, v: str | None) -> str | None:
        return _check_hosts(v)


class HyperVRead(StrictModel):
    id: uuid.UUID
    name: str
    mode: str
    host: str
    extra_hosts: str | None = None
    port: int
    use_ssl: bool
    verify_tls: bool
    username: str
    auth_method: str = "ntlm"
    kerberos_realm: str | None = None
    kerberos_kdc: str | None = None
    enabled: bool
    sync_interval_seconds: int
    scope_subnet_ids: list[str] | None = None
    auto_create_ips: bool
    description: str | None = None
    cluster_id: uuid.UUID | None = None
    last_sync_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
