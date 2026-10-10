"""public_endpoint_tokens：機櫃嵌入圖與 Graylog DSV 的權杖加密、會到期；明文 8088 要明確打開

2026-10-09 合規核對：
- 兩把權杖明文存在設定 JSON 裡、永不過期、設定頁每次載入都整把回給瀏覽器
  → 改成加密存放（綁定用途）、有到期時間（既有的給一年，到期前通知管理員）、要按「顯示」才拿得到
- Graylog DSV 的明文 8088 埠：nginx 預設就開著，權杖在網址上
  → 新增 `allow_plain_http`（預設關）；已經啟用 DSV 的站台升級後保留原本的行為（Graylog 可能
  正透過 8088 抓），由管理員在設定頁決定要不要關掉

Revision ID: 0202_public_endpoint_tokens
Revises: 0201_audit_logs_no_truncate
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision: str = "0202_public_endpoint_tokens"
down_revision: str | None = "0201_audit_logs_no_truncate"
branch_labels = None
depends_on = None

GRACE_DAYS = 365
TARGETS = (("graylog_dsv", b"setting:graylog_dsv:token"), ("rack_embed", b"setting:rack_embed:token"))


def convert(key: str, value: dict, aad: bytes, now: datetime) -> dict | None:
    """明文 token → token_enc＋到期時間；DSV 已啟用的保留明文 8088。不需要改回 None。"""
    import base64

    from app.core.security import encrypt_secret

    if not isinstance(value, dict):
        return None
    out = dict(value)
    changed = False
    plain = out.pop("token", None)
    if plain and not out.get("token_enc"):
        ct, nonce = encrypt_secret(str(plain), aad=aad)
        out["token_enc"] = "v1:" + base64.b64encode(nonce).decode() + ":" + base64.b64encode(ct).decode()
        changed = True
    elif plain is not None:
        changed = True          # 已經有密文還留著明文：拿掉明文
    if out.get("token_enc") and not out.get("token_expires_at"):
        out["token_expires_at"] = (now + timedelta(days=GRACE_DAYS)).isoformat()
        changed = True
    if key == "graylog_dsv" and "allow_plain_http" not in out:
        out["allow_plain_http"] = bool(out.get("enabled"))
        changed = True
    return out if changed else None


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.now(UTC)
    for key, aad in TARGETS:
        row = conn.execute(sa.text("SELECT value FROM system_settings WHERE key = :k"), {"k": key}).first()
        if row is None:
            continue
        new = convert(key, row[0], aad, now)
        if new is not None:
            conn.execute(sa.text("UPDATE system_settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                         {"v": json.dumps(new, ensure_ascii=False), "k": key})


def downgrade() -> None:
    """解回明文（舊版程式只認 `token`）。"""
    import base64

    from app.core.security import decrypt_secret

    conn = op.get_bind()
    for key, aad in TARGETS:
        row = conn.execute(sa.text("SELECT value FROM system_settings WHERE key = :k"), {"k": key}).first()
        if row is None or not isinstance(row[0], dict) or not row[0].get("token_enc"):
            continue
        v = dict(row[0])
        _ver, b_nonce, b_ct = str(v.pop("token_enc")).split(":", 2)
        v["token"] = decrypt_secret(base64.b64decode(b_ct), base64.b64decode(b_nonce), aad=aad).decode()
        conn.execute(sa.text("UPDATE system_settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                     {"v": json.dumps(v, ensure_ascii=False), "k": key})
