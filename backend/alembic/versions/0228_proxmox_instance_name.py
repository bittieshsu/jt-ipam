"""Proxmox VE 連線加名稱（2026-10-22）

以前沒有名稱欄位，戰情室與診斷的整合清單只能顯示「Proxmox VE」，兩台分不出來。
既有資料：有對應的虛擬化叢集就用叢集名稱，沒有就用 API 網址主機名稱的第一段。

Revision ID: 0228_proxmox_instance_name
Revises: 0227_cert_push_targets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0228_proxmox_instance_name"
down_revision: str | None = "0227_cert_push_targets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("proxmox_instances", sa.Column("name", sa.String(128)))
    op.execute("""
        UPDATE proxmox_instances p SET name = c.name
          FROM virt_clusters c WHERE c.id = p.cluster_id AND p.name IS NULL
    """)
    op.execute("""
        UPDATE proxmox_instances SET name = split_part(substring(api_url from '://([^/:]+)'), '.', 1)
         WHERE name IS NULL
    """)


def downgrade() -> None:
    op.drop_column("proxmox_instances", "name")
