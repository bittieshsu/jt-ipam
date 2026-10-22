"""異常觀測：每一筆異常的首次出現、最後出現與消失時間（戰情室的異常時序，2026-10-21）

異常偵測以前只保留「最新一份報告」（system_settings.anomaly_report），沒有歷史，
畫不出「近幾小時依類型」的時序。每次偵測完，依 anomaly._item_fingerprint 把每一筆記成一列：
新的記首次出現、還在的更新最後出現、不見的記消失時間。已消失超過保留天數的由偵測流程清掉。

Revision ID: 0224_anomaly_observations
Revises: 0223_hyperv_kerberos
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0224_anomaly_observations"
down_revision: str | None = "0223_hyperv_kerberos"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "anomaly_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(32), nullable=False),
        # 畫面上指認這一筆用的值（IP、MAC、名稱…，最多 120 字）；不是唯一識別
        sa.Column("label", sa.String(120)),
        sa.Column("ip", postgresql.INET()),
        sa.Column("subnet_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("subnets.id", ondelete="SET NULL")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    # 同一筆同時只有一列「還在」；消失後再出現是新的一列（時序上是兩段）
    op.create_index("uq_anomaly_obs_open", "anomaly_observations", ["category", "fingerprint"],
                    unique=True, postgresql_where=sa.text("resolved_at IS NULL"))
    op.create_index("ix_anomaly_obs_first_seen", "anomaly_observations", ["first_seen_at"])
    op.create_index("ix_anomaly_obs_resolved", "anomaly_observations", ["resolved_at"])
    op.create_index("ix_anomaly_obs_subnet", "anomaly_observations", ["subnet_id"])


def downgrade() -> None:
    op.drop_table("anomaly_observations")
