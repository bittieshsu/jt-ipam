"""subnets.scan_scope：大子網路改成兩層掃描（已知位址每輪掃、其餘位址背景探索）

客戶 2026-10-11：前人把主站台設成 10.0.0.0/8，掃描直接炸掉。代理以前只拿到 CIDR、每輪照順序掃 4,096 個，
/8 要約 14 天才繞一圈，上線門檻 30 分鐘 → 設備幾乎永遠被判成離線；系統的建議只有「把子網路切小」，
IPAM 只好把網段記錯。

- auto（預設）：4,096 個位址以內照舊全掃；更大的改成已知位址每輪掃＋其餘位址每輪抽一段探索
- all：一律全掃（舊行為）
- known：只掃已知位址（已登記的 IP、7 天內 ARP 看過的），不探索
- ranges：只掃這個子網路的位址範圍（ip_ranges）

既有子網路都是 auto：4,096 個以內的行為不變；更大的會在下一輪拿到新計畫（舊代理不認得計畫就照舊全掃）。

Revision ID: 0205_subnet_scan_scope
Revises: 0204_dns_compare_excluded_zones
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0205_subnet_scan_scope"
down_revision: str | None = "0204_dns_compare_excluded_zones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subnets", sa.Column("scan_scope", sa.String(16), nullable=False, server_default="auto"))
    op.create_check_constraint("ck_subnets_scan_scope", "subnets", "scan_scope IN ('auto','all','known','ranges')")


def downgrade() -> None:
    op.drop_constraint("ck_subnets_scan_scope", "subnets", type_="check")
    op.drop_column("subnets", "scan_scope")
