"""ip_addresses.device_since：目前這台設備從什麼時候開始用這個位址（使用者 2026-10-12）

一個位址換給另一台設備（MAC 變了）之後，從上一台推導出來的資料（OS 猜測、設備類型、OCS 盤點…）不再適用。
實際遇到的例子：一個位址先後給過兩台 Apple 裝置，之後換成一台 Proxmox VM，IP 頁仍顯示 OS＝macOS ——
那是 Apple 那幾個月留下的猜測，VM 擋掉所有探測，舊值永遠不會被蓋掉。當時還沒有 MAC 異動記錄，所以要回推。

這個欄位之後由程式在換手時寫（services/device_turnover.py），其他地方拿它判斷資料是不是換手前的。
回推：
  1. 異動記錄裡「目前這個 MAC 換進來」的最後一筆（舊值不是空的，而且不是 24 小時內換回剛用過的 MAC）
  2. 沒有的話：ARP 歷史裡目前這個 MAC 第一次出現在這個位址的時間 —— 只在那之前這個位址出現過別的 MAC 時
     （之前沒有別的 MAC＝一直是同一台，不設）
清掉：辨識時間早於（或沒有）換手時間的 OS、設備類型、型號；最後盤點早於換手時間的 OCS 資料。OS、類型原本有值的
在時間軸寫一筆「變成未知」。清錯了的（換手後其實辨識過、只是沒判出類型）下一輪定期偵測會再寫回來。

Revision ID: 0209_ip_device_since
Revises: 0208_isoinsight_token_in_query
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0209_ip_device_since"
down_revision: str | None = "0208_isoinsight_token_in_query"
branch_labels = None
depends_on = None

_NORM = "lower(replace(replace({}, '-', ''), ':', ''))"


def upgrade() -> None:
    op.add_column("ip_addresses", sa.Column("device_since", sa.DateTime(timezone=True)))
    cur = _NORM.format("a.mac::text")
    op.execute(f"""
        WITH arrival AS (
          SELECT DISTINCT ON (l.ip_id) l.ip_id, l.created_at AS t
            FROM ip_change_log l JOIN ip_addresses a ON a.id = l.ip_id
           WHERE l.event_type = 'mac_changed' AND l.old_value IS NOT NULL AND a.mac IS NOT NULL
             AND {_NORM.format('l.new_value')} = {cur}
           ORDER BY l.ip_id, l.created_at DESC
        ), from_log AS (
          SELECT ar.ip_id, ar.t FROM arrival ar JOIN ip_addresses a ON a.id = ar.ip_id
           WHERE NOT EXISTS (
             SELECT 1 FROM ip_change_log p
              WHERE p.ip_id = ar.ip_id AND p.event_type = 'mac_changed'
                AND {_NORM.format('p.old_value')} = {cur}
                AND p.created_at < ar.t AND p.created_at >= ar.t - interval '24 hours')
        ), from_arp AS (
          SELECT a.id AS ip_id, min(e.first_seen_at) AS t
            FROM ip_addresses a JOIN arp_entries e ON host(e.ip) = host(a.ip) AND e.mac = a.mac
           WHERE a.mac IS NOT NULL
           GROUP BY a.id
          HAVING EXISTS (
            SELECT 1 FROM arp_entries o
             WHERE host(o.ip) = host(a.ip) AND o.mac <> a.mac AND o.first_seen_at < min(e.first_seen_at))
        )
        UPDATE ip_addresses a SET device_since = coalesce(fl.t, fa.t)
          FROM ip_addresses x
          LEFT JOIN from_log fl ON fl.ip_id = x.id
          LEFT JOIN from_arp fa ON fa.ip_id = x.id
         WHERE a.id = x.id AND coalesce(fl.t, fa.t) IS NOT NULL
    """)
    # 時間軸：OS、類型原本有值、而且早於換手的，寫一筆「變成未知」
    stale_identity = ("a.device_since IS NOT NULL AND (a.device_identified_at IS NULL "
                      "OR a.device_identified_at < a.device_since)")
    op.execute(f"""
        INSERT INTO ip_change_log (id, ip_id, subnet_id, ip_text, event_type, field, old_value, new_value,
                                   source, note, created_at)
        SELECT gen_random_uuid(), a.id, a.subnet_id, host(a.ip), 'os_changed', 'os_family',
               coalesce(a.os_family, left(a.os_guess, 120)), NULL, 'system',
               'identity predates the current device (0209)', now()
          FROM ip_addresses a
         WHERE {stale_identity} AND (a.os_family IS NOT NULL OR a.os_guess IS NOT NULL)
    """)
    op.execute(f"""
        INSERT INTO ip_change_log (id, ip_id, subnet_id, ip_text, event_type, field, old_value, new_value,
                                   source, note, created_at)
        SELECT gen_random_uuid(), a.id, a.subnet_id, host(a.ip), 'kind_changed', 'device_kind',
               a.device_kind, NULL, 'system', 'identity predates the current device (0209)', now()
          FROM ip_addresses a
         WHERE {stale_identity} AND a.device_kind IS NOT NULL
    """)
    op.execute(f"""
        UPDATE ip_addresses a SET os_family = NULL, os_guess = NULL, device_kind = NULL, device_model = NULL,
               device_identified_at = NULL
         WHERE {stale_identity}
           AND (a.os_family IS NOT NULL OR a.os_guess IS NOT NULL OR a.device_kind IS NOT NULL)
    """)
    op.execute("""
        UPDATE ip_addresses a SET os_ocs = NULL, ocs_id = NULL, ocs_tag = NULL, ocs_agent = NULL,
               ocs_notes = NULL, ocs_hw = NULL, last_seen_ocs = NULL
         WHERE a.device_since IS NOT NULL AND a.last_seen_ocs IS NOT NULL AND a.last_seen_ocs < a.device_since
    """)


def downgrade() -> None:
    # 清掉的推導資料不還原（下一輪偵測會再寫回來）；只拿掉欄位
    op.drop_column("ip_addresses", "device_since")
