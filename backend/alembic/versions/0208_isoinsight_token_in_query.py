"""isoinsight_sources.token_in／token_param：Token 可以放在網址參數（原廠回信 2026-10-12）

原廠說明的呼叫方式：POST /logon（JSON）取得 SessionID，再以
GET /isosvc?act=DhcpLease&sessionid=<SessionID> 讀租約。以前 Token 只能放在標頭，照原廠方式接不上。
既有的來源維持放在標頭（header），行為不變。

Revision ID: 0208_isoinsight_token_in_query
Revises: 0207_dhcp_exclusions
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0208_isoinsight_token_in_query"
down_revision: str | None = "0207_dhcp_exclusions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("isoinsight_sources", sa.Column("token_in", sa.String(8), nullable=False, server_default="header"))
    op.add_column("isoinsight_sources", sa.Column("token_param", sa.String(64), nullable=False,
                                                  server_default="sessionid"))
    op.create_check_constraint("isoinsight_token_in", "isoinsight_sources", "token_in IN ('header','query')")


def downgrade() -> None:
    op.drop_constraint("isoinsight_token_in", "isoinsight_sources", type_="check")
    op.drop_column("isoinsight_sources", "token_param")
    op.drop_column("isoinsight_sources", "token_in")
