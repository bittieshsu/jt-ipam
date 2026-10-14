"""librenms_instances.vlan_domain_id 補索引（0211 漏了，test_fk_indexes 抓到）

外鍵欄位沒有索引時，刪一個 VLAN domain 要整表掃描 librenms_instances 找 SET NULL 的對象。
0211 已在正式站台套用過，所以另開一版補，而不是改 0211。

Revision ID: 0213_lnms_vlan_domain_idx
Revises: 0212_purge_fmt_mac_changes
"""
from __future__ import annotations

from alembic import op

revision: str = "0213_lnms_vlan_domain_idx"
down_revision: str | None = "0212_purge_fmt_mac_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_librenms_instances_vlan_domain_id", "librenms_instances", ["vlan_domain_id"],
                    if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_librenms_instances_vlan_domain_id", table_name="librenms_instances", if_exists=True)
