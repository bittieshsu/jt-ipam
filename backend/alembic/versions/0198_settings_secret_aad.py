"""settings_secret_aad：設定裡兩個沒綁定用途（AAD）的機密改成綁定

2026-10-09 合規核對：約 40 處加密機密都把用途綁進 AES-GCM 的附加資料（AAD），只有 GeoIP 授權金鑰與
phpIPAM 搬移的 SSH 私鑰沒有。沒綁定的密文可以被搬到別的欄位照樣解開；綁定之後換了位置就解不開。

這裡把既有的兩筆用舊方式解開、以綁定用途的方式重新加密。解不開的（金鑰不對、資料已損壞）原樣
留著：本來就用不了，硬改只會把診斷線索弄丟。已經是新格式的不會再動（重跑安全）。

Revision ID: 0198_settings_secret_aad
Revises: 0197_winrm_http_default
"""

from __future__ import annotations

import base64
import json

import sqlalchemy as sa
from alembic import op

revision: str = "0198_settings_secret_aad"
down_revision: str | None = "0197_winrm_http_default"
branch_labels = None
depends_on = None

#: 設定鍵 → (密文欄位, nonce 欄位, AAD)；與 services/geoip.KEY_AAD、endpoints/migration.KEY_AAD 一致
TARGETS = (
    ("geoip", "key_ct", "key_nonce", b"setting:geoip:license_key"),
    ("phpipam_migration", "key_enc", "key_nonce", b"setting:phpipam_migration:ssh_private_key"),
)


def rewrap(value: dict, enc_f: str, nonce_f: str, *, from_aad: bytes | None,
           to_aad: bytes | None) -> dict | None:
    """用 from_aad 解開、以 to_aad 重新加密；不需要或做不到時回 None（不動）。"""
    from app.core.security import decrypt_secret, encrypt_secret

    ct, nonce = value.get(enc_f), value.get(nonce_f)
    if not (isinstance(ct, str) and isinstance(nonce, str) and ct and nonce):
        return None
    try:
        plain = decrypt_secret(base64.b64decode(ct), base64.b64decode(nonce), aad=from_aad)
    except Exception:
        return None        # 已經是目標格式，或根本解不開 —— 兩種都不動
    new_ct, new_nonce = encrypt_secret(plain, aad=to_aad)
    return {**value, enc_f: base64.b64encode(new_ct).decode(), nonce_f: base64.b64encode(new_nonce).decode()}


def _apply(*, to_bound: bool) -> None:
    conn = op.get_bind()
    for key, enc_f, nonce_f, aad in TARGETS:
        row = conn.execute(sa.text("SELECT value FROM system_settings WHERE key = :k"), {"k": key}).first()
        if row is None or not isinstance(row[0], dict):
            continue
        new = rewrap(row[0], enc_f, nonce_f,
                     from_aad=None if to_bound else aad, to_aad=aad if to_bound else None)
        if new is not None:
            conn.execute(sa.text("UPDATE system_settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
                         {"v": json.dumps(new, ensure_ascii=False), "k": key})


def upgrade() -> None:
    _apply(to_bound=True)


def downgrade() -> None:
    _apply(to_bound=False)
