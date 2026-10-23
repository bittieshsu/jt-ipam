"""OpenStack／Bigstack CubeCOS 整合（唯讀，Beta）

- 新增 openstack_instances（Keystone v3 連線、選用的 CubeCOS API）
- 新增 openstack_ports（Neutron port 鏡像：配置 IP、binding 主機、Floating IP）
- 新增 openstack_subnets（Neutron 子網路與對應的 jt-ipam 子網路；手動對應不被同步覆蓋）
- virt_clusters.type 加 'openstack'
- ip_addresses.discovery_source 加 'openstack'：依 OpenStack 回報自動建立的 IP 看得出是誰建的

Revision ID: 0229_openstack
Revises: 0228_proxmox_instance_name
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0229_openstack"
down_revision: str | None = "0228_proxmox_instance_name"
branch_labels: str | None = None
depends_on: str | None = None

_IP_OLD = ("discovery_source IN ('manual','scanner','librenms','dns','proxmox','opnsense',"
           "'phpipam','pfsense','vmware','librenms_arp','isoinsight','snmp_arp','hyperv')")
_IP_NEW = ("discovery_source IN ('manual','scanner','librenms','dns','proxmox','opnsense',"
           "'phpipam','pfsense','vmware','librenms_arp','isoinsight','snmp_arp','hyperv','openstack')")
_IP_NAMES = (
    "ip_discovery_source_valid",
    "ck_ip_addresses_ip_discovery_source_valid",
    "ck_ip_addresses_ck_ip_addresses_ip_discovery_source_valid",
)
_IP_CANON = "ck_ip_addresses_ip_discovery_source_valid"

_VC_OLD = "type IN ('proxmox','vmware','hyper-v','kvm','xenserver','other')"
_VC_NEW = "type IN ('proxmox','vmware','hyper-v','kvm','xenserver','openstack','other')"
# 舊站台的約束名稱有兩種寫法（命名慣例套用前後），都要清掉
_VC_NAMES = (
    "ck_virt_clusters_type_valid",
    "ck_virt_clusters_ck_virt_clusters_type_valid",
)
_VC_CANON = "ck_virt_clusters_ck_virt_clusters_type_valid"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "openstack_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("product", sa.String(16), nullable=False, server_default="openstack"),
        sa.Column("auth_url", sa.Text(), nullable=False),
        sa.Column("auth_method", sa.String(16), nullable=False, server_default="password"),
        sa.Column("username", sa.String(255)),
        sa.Column("user_domain", sa.String(255), nullable=False, server_default="Default"),
        sa.Column("project_name", sa.String(255)),
        sa.Column("project_domain", sa.String(255), nullable=False, server_default="Default"),
        sa.Column("app_credential_id", sa.String(255)),
        sa.Column("secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("region", sa.String(255)),
        sa.Column("interface", sa.String(16), nullable=False, server_default="public"),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("ca_cert", sa.Text()),
        sa.Column("all_projects", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cubecos_url", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True)),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_full_success_at", sa.DateTime(timezone=True)),
        sa.Column("scope_subnet_ids", postgresql.JSONB()),
        sa.Column("auto_create_ips", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text()),
        sa.Column("capabilities", postgresql.JSONB()),
        sa.Column("nodes", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint("product IN ('openstack','cubecos')", name="ck_openstack_instances_product"),
        sa.CheckConstraint("auth_method IN ('password','app_credential')", name="ck_openstack_instances_auth"),
        sa.CheckConstraint("interface IN ('public','internal','admin')", name="ck_openstack_instances_interface"),
    )
    op.create_table(
        "openstack_ports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("openstack_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("port_id", sa.String(64), nullable=False),
        sa.Column("vm_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("virtual_machines.id", ondelete="SET NULL")),
        sa.Column("device_id", sa.String(64)),
        sa.Column("device_owner", sa.String(64)),
        sa.Column("network_id", sa.String(64)),
        sa.Column("network_name", sa.Text()),
        sa.Column("project_id", sa.String(64)),
        sa.Column("project_name", sa.Text()),
        sa.Column("mac", postgresql.MACADDR()),
        sa.Column("fixed_ips", postgresql.JSONB()),
        sa.Column("status", sa.String(16)),
        sa.Column("binding_host", sa.Text()),
        sa.Column("floating_ips", postgresql.JSONB()),
        sa.Column("missing_syncs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("instance_id", "port_id", name="uq_openstack_ports_instance_port"),
    )
    op.create_index("ix_openstack_ports_vm_id", "openstack_ports", ["vm_id"],
                    postgresql_where=sa.text("vm_id IS NOT NULL"))
    op.create_table(
        "openstack_subnets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("openstack_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subnet_id", sa.String(64), nullable=False),
        sa.Column("network_id", sa.String(64)),
        sa.Column("network_name", sa.Text()),
        sa.Column("name", sa.Text()),
        sa.Column("project_id", sa.String(64)),
        sa.Column("project_name", sa.Text()),
        sa.Column("cidr", postgresql.CIDR(), nullable=False),
        sa.Column("ip_version", sa.Integer()),
        sa.Column("gateway_ip", postgresql.INET()),
        sa.Column("allocation_pools", postgresql.JSONB()),
        sa.Column("enable_dhcp", sa.Boolean()),
        sa.Column("shared", sa.Boolean()),
        sa.Column("mapped_subnet_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("subnets.id", ondelete="SET NULL")),
        sa.Column("mapped_manually", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("missing_syncs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("instance_id", "subnet_id", name="uq_openstack_subnets_instance_subnet"),
    )
    op.create_index("ix_openstack_subnets_mapped_subnet_id", "openstack_subnets", ["mapped_subnet_id"],
                    postgresql_where=sa.text("mapped_subnet_id IS NOT NULL"))

    for n in _VC_NAMES:
        op.execute(f'ALTER TABLE virt_clusters DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE virt_clusters ADD CONSTRAINT "{_VC_CANON}" CHECK ({_VC_NEW})')
    for n in _IP_NAMES:
        op.execute(f'ALTER TABLE ip_addresses DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE ip_addresses ADD CONSTRAINT "{_IP_CANON}" CHECK ({_IP_NEW})')


def downgrade() -> None:
    op.execute("UPDATE ip_addresses SET discovery_source='scanner' WHERE discovery_source='openstack'")
    for n in _IP_NAMES:
        op.execute(f'ALTER TABLE ip_addresses DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE ip_addresses ADD CONSTRAINT "{_IP_CANON}" CHECK ({_IP_OLD})')
    # OpenStack 叢集與它的 VM（網卡 CASCADE）一併移除，否則舊約束加不回去
    op.execute("DELETE FROM virt_clusters WHERE type = 'openstack'")
    for n in _VC_NAMES:
        op.execute(f'ALTER TABLE virt_clusters DROP CONSTRAINT IF EXISTS "{n}"')
    op.execute(f'ALTER TABLE virt_clusters ADD CONSTRAINT "{_VC_CANON}" CHECK ({_VC_OLD})')
    op.drop_index("ix_openstack_subnets_mapped_subnet_id", table_name="openstack_subnets")
    op.drop_table("openstack_subnets")
    op.drop_index("ix_openstack_ports_vm_id", table_name="openstack_ports")
    op.drop_table("openstack_ports")
    op.drop_table("openstack_instances")
