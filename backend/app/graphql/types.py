"""GraphQL 型別定義。

對應 SQLAlchemy ORM model；nested resolver 在 schema.py。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import strawberry


@strawberry.type
class Section:
    id: uuid.UUID
    name: str
    description: str | None
    parent_id: uuid.UUID | None
    strict_mode: bool
    display_order: int
    created_at: datetime
    updated_at: datetime


@strawberry.type
class IPAddress:
    id: uuid.UUID
    subnet_id: uuid.UUID
    ip: str
    hostname: str | None
    description: str | None
    state: str
    mac: str | None
    owner: str | None
    discovery_source: str
    effective_status: str | None
    created_at: datetime
    updated_at: datetime


@strawberry.type
class SubnetUsage:
    total: int
    used: int
    free: int
    used_pct: float


@strawberry.type
class Subnet:
    id: uuid.UUID
    section_id: uuid.UUID
    master_subnet_id: uuid.UUID | None
    cidr: str
    description: str | None
    vlan_id: uuid.UUID | None
    vrf_id: uuid.UUID | None
    is_pool: bool
    is_full: bool
    scan_enabled: bool
    auto_dns: bool
    created_at: datetime
    updated_at: datetime


@strawberry.type
class Device:
    id: uuid.UUID
    name: str
    type: str
    vendor: str | None
    model: str | None
    serial: str | None
    primary_ip_id: uuid.UUID | None
    location_id: uuid.UUID | None
    rack_id: uuid.UUID | None
    u_position: int | None
    u_size: int | None
    description: str | None
    created_at: datetime
    updated_at: datetime


@strawberry.type
class VLAN:
    id: uuid.UUID
    domain_id: uuid.UUID
    number: int
    name: str
    description: str | None


@strawberry.type
class ARPLookup:
    """IP → MAC → switch port 推導結果。"""

    ip: str
    mac: str | None
    interface: str | None
    switch_device_id: uuid.UUID | None = strawberry.field(
        description="LibreNMS 裝置鏡像（librenms_devices）的 ID —— 不是 jt-ipam 的裝置；"
                    "要 jt-ipam 裝置請用 switchIpamDeviceId")
    switch_port: str | None
    vlan: int | None
    switch_name: str | None = strawberry.field(default=None, description="交換器名稱（sysName／hostname）")
    switch_ipam_device_id: uuid.UUID | None = strawberry.field(
        default=None, description="交換器對映到的 jt-ipam 裝置 ID（沒有對映時為 null）")
