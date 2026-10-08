"""impact_ai_stage：IP 變更評估的 AI 產出記下做到哪一步

使用者 2026-10-08：按下 AI 後只有轉圈，要顯示正在做什麼。impact_ai_artifacts.stage
（collecting／asking／checking／retrying，做完清掉），畫面輪詢時顯示。

Revision ID: 0193_impact_ai_stage
Revises: 0192_technitium
"""

from __future__ import annotations

from alembic import op

revision: str = "0193_impact_ai_stage"
down_revision: str | None = "0192_technitium"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE impact_ai_artifacts ADD COLUMN IF NOT EXISTS stage VARCHAR(24)")


def downgrade() -> None:
    op.execute("ALTER TABLE impact_ai_artifacts DROP COLUMN IF EXISTS stage")
