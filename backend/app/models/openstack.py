"""Bigstack CubeCOS／OpenStack 整合（唯讀，Beta；0229）。

一個連線＝一個 Keystone v3 端點加一組服務身分。VM 與網卡寫進**共用的虛擬化資料表**
（`virt_clusters` / `virtual_machines` / `vm_interfaces`，叢集類型 `openstack`），拓樸、AI 對話、MCP 的
`list_vms` 不必改。OpenStack 特有的資料另外存兩張表：

- `openstack_ports`：Neutron 的每一個 port（VM 網卡、路由器、DHCP…都有），保留 port／network／project 的 UUID、
  配置 IP（fixed_ips）、binding 主機與綁在上面的 Floating IP。Floating IP **不是**客體網卡的 IP，只存不連 IPAM。
- `openstack_subnets`：Neutron 子網路與它對應到的 jt-ipam 子網路。自動對應只在「範圍內剛好一個同 CIDR 的子網路」
  時才做；不同 project 的重疊網段、對不到或對到好幾個都留「待對應」，使用者手動指定的永遠不被同步覆蓋。

刪除規則（與其他整合「完整才清」一致）：清單沒抓完（分頁中斷、403、逾時）不刪也不累計；
連續兩次完整同步都沒出現才刪 port／子網路列。
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
from sqlalchemy.dialects.postgresql import CIDR, INET, JSONB, MACADDR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

OPENSTACK_PRODUCTS = ("openstack", "cubecos")
OPENSTACK_AUTH_METHODS = ("password", "app_credential")
OPENSTACK_INTERFACES = ("public", "internal", "admin")


class OpenStackInstance(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "openstack_instances"
    __table_args__ = (
        CheckConstraint("product IN ('openstack','cubecos')", name="ck_openstack_instances_product"),
        CheckConstraint("auth_method IN ('password','app_credential')", name="ck_openstack_instances_auth"),
        CheckConstraint("interface IN ('public','internal','admin')", name="ck_openstack_instances_interface"),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # openstack：一般 OpenStack；cubecos：另外讀 CubeCOS API 的節點清單
    product: Mapped[str] = mapped_column(String(16), nullable=False, default="openstack", server_default="openstack")
    # Keystone v3 端點（有沒有 /v3 都可以）
    auth_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False, default="password", server_default="password")
    username: Mapped[str | None] = mapped_column(String(255))
    user_domain: Mapped[str] = mapped_column(String(255), nullable=False, default="Default", server_default="Default")
    project_name: Mapped[str | None] = mapped_column(String(255))
    project_domain: Mapped[str] = mapped_column(String(255), nullable=False, default="Default",
                                                server_default="Default")
    app_credential_id: Mapped[str | None] = mapped_column(String(255))
    # 密碼或 Application Credential 的 secret（AES-GCM，AAD openstack_instance:<id>:secret）
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 留空＝用 Service Catalog 裡第一個看到的 region
    region: Mapped[str | None] = mapped_column(String(255))
    interface: Mapped[str] = mapped_column(String(16), nullable=False, default="public", server_default="public")
    verify_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # 內部 CA（PEM）。有設就只信任它
    ca_cert: Mapped[str | None] = mapped_column(Text)
    # 嘗試跨 project（Nova all_tenants=1）；被拒就退回 token 所屬的 project，並記在 capabilities
    all_projects: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # 只在 product='cubecos' 時用；留空＝https://<auth_url 的主機>
    cubecos_url: Mapped[str | None] = mapped_column(Text)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # 對映到哪個叢集紀錄（第一次同步時建立，之後沿用）；跟 Hyper-V 一樣不設外鍵
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    sync_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300, server_default="300")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # 最後一次「所有清單都完整抓完」的時間（刪除判定與資料時效用）
    last_full_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 限定子網路範圍：自動對應子網路與 IP 比對都只在這些子網路內找
    scope_subnet_ids: Mapped[list[Any] | None] = mapped_column(JSONB)
    # 已對應子網路裡 IPAM 還沒有的位址：自動建立。預設關閉（同其他虛擬化整合）
    auto_create_ips: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                  server_default=text("false"))
    description: Mapped[str | None] = mapped_column(Text)
    # 最後一次測試／同步得到的能力（各服務可不可達、跨 project、主機欄位看不看得到…）
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # CubeCOS 節點（最後一次成功讀到的）
    nodes: Mapped[list[Any] | None] = mapped_column(JSONB)


class OpenStackPort(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "openstack_ports"
    __table_args__ = (
        UniqueConstraint("instance_id", "port_id", name="uq_openstack_ports_instance_port"),
        Index("ix_openstack_ports_vm_id", "vm_id", postgresql_where=text("vm_id IS NOT NULL")),
    )

    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("openstack_instances.id", ondelete="CASCADE"), nullable=False)
    port_id: Mapped[str] = mapped_column(String(64), nullable=False)
    vm_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("virtual_machines.id", ondelete="SET NULL"))
    device_id: Mapped[str | None] = mapped_column(String(64))
    device_owner: Mapped[str | None] = mapped_column(String(64))
    network_id: Mapped[str | None] = mapped_column(String(64))
    network_name: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(String(64))
    project_name: Mapped[str | None] = mapped_column(Text)
    mac: Mapped[str | None] = mapped_column(MACADDR)
    # [{"subnet_id", "ip_address"}]：控制平面的配置，不代表客體已套用或在線
    fixed_ips: Mapped[list[Any] | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String(16))
    # Neutron binding:host_id；權限不夠看不到時是 null（不是「沒有」）
    binding_host: Mapped[str | None] = mapped_column(Text)
    # [{"floating_ip_address", "floating_ip_id", "router_id"}]：外部位址，不是客體網卡的 IP
    floating_ips: Mapped[list[Any] | None] = mapped_column(JSONB)
    missing_syncs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                    server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                   server_default=text("now()"))


class OpenStackSubnet(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "openstack_subnets"
    __table_args__ = (
        UniqueConstraint("instance_id", "subnet_id", name="uq_openstack_subnets_instance_subnet"),
        Index("ix_openstack_subnets_mapped_subnet_id", "mapped_subnet_id",
              postgresql_where=text("mapped_subnet_id IS NOT NULL")),
    )

    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("openstack_instances.id", ondelete="CASCADE"), nullable=False)
    subnet_id: Mapped[str] = mapped_column(String(64), nullable=False)
    network_id: Mapped[str | None] = mapped_column(String(64))
    network_name: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(String(64))
    project_name: Mapped[str | None] = mapped_column(Text)
    cidr: Mapped[str] = mapped_column(CIDR, nullable=False)
    ip_version: Mapped[int | None] = mapped_column(Integer)
    gateway_ip: Mapped[str | None] = mapped_column(INET)
    allocation_pools: Mapped[list[Any] | None] = mapped_column(JSONB)
    enable_dhcp: Mapped[bool | None] = mapped_column(Boolean)
    shared: Mapped[bool | None] = mapped_column(Boolean)
    # 對應到的 jt-ipam 子網路；null＝待對應
    mapped_subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subnets.id", ondelete="SET NULL"))
    # 使用者手動指定（含手動設成「不對應」）：同步永遠不覆蓋
    mapped_manually: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                  server_default=text("false"))
    missing_syncs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                    server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                   server_default=text("now()"))
