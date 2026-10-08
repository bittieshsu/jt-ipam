"""isoinsight_schedule_on：ISOinsight 排程預設開啟

使用者 2026-10-08：排程預設要開，只是還沒成功預覽過時不會同步（job.run_due 記「要先預覽」）。
以前開排程要先成功預覽，所以「沒預覽過、排程關著」的來源不是管理員選的，是當時開不了：一併打開。
預覽成功過、排程關著的可能是刻意關的，不動。

Revision ID: 0191_isoinsight_schedule_on
Revises: 0190_change_impact_review_steps
"""

from __future__ import annotations

from alembic import op

revision: str = "0191_isoinsight_schedule_on"
down_revision: str | None = "0190_change_impact_review_steps"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE isoinsight_sources ALTER COLUMN schedule_enabled SET DEFAULT true")
    op.execute("UPDATE isoinsight_sources SET schedule_enabled = true "
               "WHERE schedule_enabled = false AND preview_ok_at IS NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE isoinsight_sources ALTER COLUMN schedule_enabled SET DEFAULT false")
