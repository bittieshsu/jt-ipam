"""IP 換給另一台設備（MAC 變了）：清掉從上一台推導出來的資料（使用者 2026-10-12）。

正式環境：一個 DHCP 位址先後給過 iPhone、iPad、MacBook，後來換成一台 Proxmox VM。IP 頁仍顯示 OS＝macOS ——
那是很久以前 nmap 對上一台的猜測。以前 MAC 換了只清「跟著設備走的名稱」，辨識結果不清；新的 VM 擋掉所有探測，
舊值就永遠不會被蓋掉，而且下游（自動判斷裝置類型、異常偵測、清單的 OS 欄）都拿它當真。

清的是**描述上一台設備、由偵測推導**的資料，新設備偵測得到時會再寫回來；人填的（擁有者、說明、備註、裝置連結、
主控台設定…）不動。SSH 主機金鑰的釘選也不動：MAC 被偽造時，那正是要擋的情況。
每一條會改 MAC 的路徑都呼叫 `on_device_replaced`：同步（arp_precedence）、手動編輯、MCP、phpIPAM 相容 API。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import IPAddress

#: 辨識結果：掃描代理定期 OS 偵測／IP 探測推導的
IDENTITY_FIELDS = ("os_family", "os_guess", "device_kind", "device_model", "device_identified_at")
#: OCS 盤點：OCS 是照 MAC 對到 IP 的，MAC 換了這些必然屬於上一台（增量同步之後不會再有完整比對來清它）
OCS_FIELDS = ("os_ocs", "ocs_id", "ocs_tag", "ocs_agent", "ocs_notes", "ocs_hw", "last_seen_ocs")


async def forget_device_identity(session: AsyncSession, *, ip: IPAddress, source: str,
                                 actor_user_id: str | uuid.UUID | None = None) -> None:
    """清掉上一台的辨識結果與 OCS 盤點資料；OS、類型原本有值的，在時間軸寫一筆「變成未知」。"""
    from app.services.ip_history import log_change

    if ip.os_family or ip.os_guess:
        await log_change(session, ip=ip, event_type="os_changed", field="os_family",
                         old=ip.os_family or (ip.os_guess or "")[:120], new=None,
                         source=source, actor_user_id=actor_user_id)
    if ip.device_kind:
        await log_change(session, ip=ip, event_type="kind_changed", field="device_kind",
                         old=ip.device_kind, new=None, source=source, actor_user_id=actor_user_id)
    for field in (*IDENTITY_FIELDS, *OCS_FIELDS):
        setattr(ip, field, None)


async def on_device_replaced(session: AsyncSession, *, ip: IPAddress, source: str,
                             actor_user_id: str | uuid.UUID | None = None) -> None:
    """這個位址換了一台設備：上一台的辨識結果、OCS 盤點、自己報的名稱都不再適用。"""
    from datetime import UTC, datetime

    from app.services.hostname import forget_device_names

    ip.device_since = datetime.now(UTC)
    await forget_device_identity(session, ip=ip, source=source, actor_user_id=actor_user_id)
    await forget_device_names(session, ip=ip, source=source)
