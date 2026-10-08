"""change_impact_review_steps：IP 變更評估的審核關卡

「申請審核設定」裡 IP 變更評估可以設定多組會簽或依序多關卡（比照 IP 申請審核）：
- impact_reviews.step_index：這一筆核准屬於第幾關（單一關卡的模式為 NULL）
- change_plans.submitted_at：最近一次送審的時間；重新送審後，之前通過的關卡不算

Revision ID: 0190_change_impact_review_steps
Revises: 0189_change_impact_services
"""

from __future__ import annotations

from alembic import op

revision: str = "0190_change_impact_review_steps"
down_revision: str | None = "0189_change_impact_services"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE impact_reviews ADD COLUMN IF NOT EXISTS step_index INTEGER")
    op.execute("ALTER TABLE change_plans ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    op.execute("ALTER TABLE change_plans DROP COLUMN IF EXISTS submitted_at")
    op.execute("ALTER TABLE impact_reviews DROP COLUMN IF EXISTS step_index")
