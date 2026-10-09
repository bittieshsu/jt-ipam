"""winrm_http_default：Windows DHCP／Windows DNS 的 WinRM 預設改走 HTTP 5985

使用者 2026-10-09：「預設 5986 這個改掉，設定頁也是，預設走 5985 http」—— Windows Server 的防火牆
預設只開 WinRM HTTP 5985，HTTPS 5986 要另外建立憑證監聽與防火牆規則。走 HTTP 時內容與帳密一律以
NTLM 加密（程式裡 message_encryption=always）。

- windows_dhcp_servers：port／use_ssl 的資料庫預設值改成 5985／false（既有的連線不動）
- dns_servers（windows_dns）：設定裡沒記錄傳輸方式的，當時程式一律走 HTTPS 5986；現在沒記錄＝HTTP，
  所以先把這些既有伺服器明確寫成 use_ssl=true，升級後照舊走 HTTPS，不會被切換

Revision ID: 0197_winrm_http_default
Revises: 0196_checkpoint_gaia_scripts_on
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision: str = "0197_winrm_http_default"
down_revision: str | None = "0196_checkpoint_gaia_scripts_on"
branch_labels = None
depends_on = None


def pin_legacy_https(extra_text: str | None) -> str | None:
    """沒記錄 use_ssl 的設定 → 補上 use_ssl=true 後的 JSON 字串；已經有的、或 JSON 壞掉的回 None（不動）。"""
    if not extra_text or not extra_text.strip():
        return json.dumps({"use_ssl": True})
    try:
        extra = json.loads(extra_text)
    except ValueError:
        return None
    if not isinstance(extra, dict) or "use_ssl" in extra:
        return None
    return json.dumps({**extra, "use_ssl": True}, ensure_ascii=False)


def upgrade() -> None:
    op.alter_column("windows_dhcp_servers", "port", server_default="5985")
    op.alter_column("windows_dhcp_servers", "use_ssl", server_default=sa.false())
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, extra_config FROM dns_servers WHERE type = 'windows_dns'")).all()
    for rid, extra_text in rows:
        pinned = pin_legacy_https(extra_text)
        if pinned is not None:
            conn.execute(sa.text("UPDATE dns_servers SET extra_config = :e WHERE id = :i"), {"e": pinned, "i": rid})


def downgrade() -> None:
    # 寫進 Windows DNS 的 use_ssl=true 留著（舊版本來就當成 HTTPS，結果一樣）
    op.alter_column("windows_dhcp_servers", "port", server_default="5986")
    op.alter_column("windows_dhcp_servers", "use_ssl", server_default=sa.true())
