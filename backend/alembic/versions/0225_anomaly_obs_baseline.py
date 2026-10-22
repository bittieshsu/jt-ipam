"""異常觀測加 baseline：觀測表第一次記錄時就已經存在的發現（2026-10-22）

0224 部署當下，報告裡既有的每一筆都被記成「那一刻首次出現」，戰情室時序在那一格畫出整排「新出現」、
上方寫「6 小時內新出現 149」。它們早就存在，只是那時才開始記錄；真正的首次出現時間不知道。
既有資料：首次出現等於全表最早時間的那一批（第一次記錄）補標 baseline。

Revision ID: 0225_anomaly_obs_baseline
Revises: 0224_anomaly_observations
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0225_anomaly_obs_baseline"
down_revision: str | None = "0224_anomaly_observations"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("anomaly_observations",
                  sa.Column("baseline", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.execute("UPDATE anomaly_observations SET baseline = true "
               "WHERE first_seen_at = (SELECT min(first_seen_at) FROM anomaly_observations)")


def downgrade() -> None:
    op.drop_column("anomaly_observations", "baseline")
