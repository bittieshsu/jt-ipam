"""用網卡 MAC 把 IP 掛回它所屬的裝置。

由來（2026-08-06 實機）：一台雙網卡機器的第二個 IP 沒有 `device_id`，於是裝置頁的
「IP 清單」只列得出一筆，AI 巡檢也據此把它報成「重複的 IP 紀錄」。但那個 MAC 就寫在
該裝置的 eth1 連接埠上 —— 系統手上早就有答案，只是沒去用。

**寧可不掛也不要掛錯。** 這是本專案在整合同步上一貫的原則：沒有關聯，使用者會去查；
掛錯了，不會有人發現。所以：
- 同一個 MAC 對到多台裝置 → 不猜，跳過（虛擬橋接、bond 的 MAC 很容易撞）
- 已經有 `device_id` 的 → 一律不動（人工指定過的不能被自動邏輯改掉）
- 永遠只新增關聯，**不會**把既有的關聯拿掉
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import IPAddress
from app.models.physical import DevicePort
from app.services.arp_precedence import normalize_mac


async def link_by_port_mac(session: AsyncSession, *, dry_run: bool = False) -> int:
    """把沒有裝置關聯、但 MAC 對得到某台裝置連接埠的 IP 掛上去。回傳筆數。

    `dry_run=True` 只計算不寫入 —— 這是會改資料的動作，要能先看會動到什麼。
    """
    port_rows = (await session.execute(
        select(DevicePort.device_id, DevicePort.mac_address)
        .where(DevicePort.mac_address.isnot(None))
    )).all()

    # MAC → 裝置集合。用集合而不是 dict，才看得出「同一個 MAC 對到多台」這件事；
    # 用 dict 的話後面的列會無聲蓋掉前面的，而那正是猜錯的來源。
    by_mac: dict[str, set] = {}
    for dev_id, mac in port_rows:
        key = normalize_mac(mac)
        if key:
            by_mac.setdefault(key, set()).add(dev_id)
    unique = {m: next(iter(d)) for m, d in by_mac.items() if len(d) == 1}
    if not unique:
        return 0

    rows = (await session.execute(
        select(IPAddress).where(IPAddress.device_id.is_(None), IPAddress.mac.isnot(None))
    )).scalars().all()

    from app.services.ip_history import log_change

    n = 0
    for ipa in rows:
        dev_id = unique.get(normalize_mac(ipa.mac))
        if not dev_id:
            continue
        n += 1
        if dry_run:
            continue
        ipa.device_id = dev_id
        # 留痕：使用者看到裝置欄突然有值，要查得到是什麼時候、依據什麼掛上的
        await log_change(
            session, ip=ipa, event_type="edited", field="device_id",
            old=None, new=str(dev_id), source="system",
            note="matched a device port MAC",
        )
    return n
