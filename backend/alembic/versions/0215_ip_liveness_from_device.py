"""ip_addresses.liveness_from_device／device_evidence_ip：上線狀態跟著所屬設備（客戶 2026-10-14）

多 LAN 路由器的閘道（路由器自己的介面位址）不回 ping 但一直在用。勾了 liveness_from_device：
每輪把同一台設備其他位址最新的會過期證據搬到 arp_seen["device"]（services/device_liveness），
device_evidence_ip 記是哪一個位址提供的；沒掛設備時不算離線（顯示「使用中（不回應探測）」）。

Revision ID: 0215_ip_liveness_from_device
Revises: 0214_dns_compare_types
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET

revision: str = "0215_ip_liveness_from_device"
down_revision: str | None = "0214_dns_compare_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ip_addresses", sa.Column("liveness_from_device", sa.Boolean(), nullable=False,
                                            server_default=sa.text("false")))
    op.add_column("ip_addresses", sa.Column("device_evidence_ip", INET(), nullable=True))


def downgrade() -> None:
    op.drop_column("ip_addresses", "device_evidence_ip")
    op.drop_column("ip_addresses", "liveness_from_device")
