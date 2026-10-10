"""token_expiry_and_cleanup：拿掉沒作用的 object_filters、對外 MCP 金鑰加上到期時間

2026-10-09 合規核對：
- `api_tokens.object_filters` 可以填、會存，卻沒有任何程式讀它 —— 填了以為權杖被限制在某些物件，
  實際上照樣沿用擁有者的完整權限。拿掉欄位；要限制範圍請另建低權限帳號再用它建權杖。
- 對外 MCP 金鑰（jtmcp_）不會過期。新金鑰輪替時選有效天數；既有的金鑰不可以升級完就失效
  （外部的 MCP 用戶端會突然全部斷掉），所以給 90 天，到期前照常通知管理員去輪替。

Revision ID: 0199_token_expiry_and_cleanup
Revises: 0198_settings_secret_aad
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0199_token_expiry_and_cleanup"
down_revision: str | None = "0198_settings_secret_aad"
branch_labels = None
depends_on = None

#: 既有金鑰升級後的寬限天數
GRACE_DAYS = 90


def with_default_expiry(value: dict, key_field: str, exp_field: str, now: datetime) -> dict | None:
    """有金鑰、沒有到期時間 → 補上 now＋寬限天數；其他情況回 None（不動）。"""
    if not isinstance(value, dict) or not value.get(key_field) or value.get(exp_field):
        return None
    return {**value, exp_field: (now + timedelta(days=GRACE_DAYS)).isoformat()}


def upgrade() -> None:
    op.drop_column("api_tokens", "object_filters")
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT value FROM system_settings WHERE key = 'llm'")).first()
    if row is not None:
        new = with_default_expiry(row[0], "mcp_api_key_enc", "mcp_api_key_expires_at", datetime.now(UTC))
        if new is not None:
            conn.execute(sa.text("UPDATE system_settings SET value = CAST(:v AS jsonb) WHERE key = 'llm'"),
                         {"v": json.dumps(new, ensure_ascii=False)})


def downgrade() -> None:
    op.add_column("api_tokens", sa.Column("object_filters", postgresql.JSONB(), nullable=True))
