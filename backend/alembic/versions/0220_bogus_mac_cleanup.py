"""清掉兩種假 MAC（2026-10-18 外部 AI 實測 MCP 回報）

1. 沒人輸入過的「手動」MAC：2026-09-26 以前，IP 編輯表單每次存檔都把當時顯示的 MAC 一起送出，後端就把
   mac_source 標成 manual（優先序最高）—— DHCP 位址當時是哪台，就永遠停在那台，之後租約、ARP、掃描看到
   新的 MAC 也寫不進來（正式環境 45 筆，沒有一筆真的有人改過 MAC）。表單那邊 09-26 已修、舊資料沒清。
   依稽核記錄判斷：有「MAC 沒變卻一起送出」的存檔記錄、而且找不到任何真的輸入 MAC 的記錄（新增時帶 MAC、
   改成不同的 MAC、舊格式沒有 before 的存檔、phpIPAM 相容 API 的寫入）→ 來源改成未知（優先序最低、
   任何來源都能覆寫）。MAC 本身不動；沒有稽核記錄的（例如從別台匯入）不猜，維持手動。
2. ARP 觀測裡的殘缺 MAC：LibreNMS 偶爾給「只有第一個位元組」的值（c4:00:00:00:00:00），全零、廣播也沒擋；
   寫入端（arp_evidence.normalize、LibreNMS ARP 同步）已經不收，這裡刪掉已經存下來的。

不可逆：downgrade 不還原（無從得知哪些是被清掉的，也不該把錯的資料放回去）。

Revision ID: 0220_bogus_mac_cleanup
Revises: 0219_user_pref_dashboard
"""
from __future__ import annotations

from alembic import op

revision: str = "0220_bogus_mac_cleanup"
down_revision: str | None = "0219_user_pref_dashboard"
branch_labels = None
depends_on = None

UPGRADE_SQL: list[str] = [
    """
    UPDATE ip_addresses a SET mac_source = NULL
     WHERE a.mac_source = 'manual'
       AND EXISTS (
           SELECT 1 FROM audit_logs l
            WHERE l.object_id = a.id AND l.action = 'update' AND l.diff ? 'before'
              AND l.diff->'changes' ? 'mac'
              AND lower(regexp_replace(coalesce(l.diff->'changes'->>'mac', ''), '[^0-9a-fA-F]', '', 'g'))
                = lower(regexp_replace(coalesce(l.diff->'before'->>'mac', ''), '[^0-9a-fA-F]', '', 'g')))
       AND NOT EXISTS (
           SELECT 1 FROM audit_logs l
            WHERE l.object_id = a.id AND (
                  l.diff->>'via' = 'phpipam'
               OR (l.action = 'create' AND coalesce(l.diff->'after'->>'mac', l.diff->'changes'->>'mac') IS NOT NULL)
               OR (l.diff->'changes' ? 'mac' AND NOT l.diff ? 'before')
               OR (l.diff->'changes' ? 'mac'
                   AND lower(regexp_replace(coalesce(l.diff->'changes'->>'mac', ''), '[^0-9a-fA-F]', '', 'g'))
                    <> lower(regexp_replace(coalesce(l.diff->'before'->>'mac', ''), '[^0-9a-fA-F]', '', 'g')))))
    """,
    """
    DELETE FROM arp_entries
     WHERE substr(replace(mac::text, ':', ''), 3) = '0000000000'
        OR mac::text = 'ff:ff:ff:ff:ff:ff'
    """,
]


def upgrade() -> None:
    for stmt in UPGRADE_SQL:
        op.execute(stmt)


def downgrade() -> None:
    pass
