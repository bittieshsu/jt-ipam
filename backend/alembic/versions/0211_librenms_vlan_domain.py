"""librenms_instances.vlan_domain_id／vlan_create_missing：VLAN 同步寫進指定的 domain（GitHub issue #53）

以前固定寫進名為「LibreNMS」的 domain，從 phpIPAM 搬來、子網路接在 default domain 的 VLAN 因此出現兩份。
指定 domain 後沿用既有 VLAN（只補 device↔VLAN 對應、不覆寫已有的名稱）；vlan_create_missing 關掉就只對應既有的。
不指定（NULL）＝維持原本的行為。指定的 domain 被刪掉時改回 NULL（ON DELETE SET NULL）。

Revision ID: 0211_librenms_vlan_domain
Revises: 0210_dhcp_pool_configured_range
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0211_librenms_vlan_domain"
down_revision: str | None = "0210_dhcp_pool_configured_range"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("librenms_instances", sa.Column(
        "vlan_domain_id", UUID(as_uuid=True), sa.ForeignKey("vlan_domains.id", ondelete="SET NULL"), nullable=True))
    op.add_column("librenms_instances", sa.Column(
        "vlan_create_missing", sa.Boolean(), nullable=False, server_default=sa.text("true")))


def downgrade() -> None:
    op.drop_column("librenms_instances", "vlan_create_missing")
    op.drop_column("librenms_instances", "vlan_domain_id")
