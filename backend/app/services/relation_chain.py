"""關係鏈的共用判斷（IP 頁、裝置頁的關係圖與戰情室「IP 物件關係」同一套規則）。

以前這段在 addresses.py 與 devices.py 各寫一份；戰情室 09 也要用，抽出來避免三份各自漂移。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def vm_host_device(session: AsyncSession, vm: Any) -> tuple[Any | None, str | None]:
    """VM 所在的實體節點對到哪一台 jt-ipam 裝置：(裝置, 依據)。

    依據：linked（VM 記錄直接連到裝置）、name（節點名稱＝裝置名稱）、fqdn（節點名稱＝裝置 FQDN）。
    名稱比對是推導，不是來源直接回報；呼叫端要分得出來（戰情室畫虛線）。對不到回 (None, None)。
    """
    from app.models.device import Device

    if vm.device_id:
        dev = await session.get(Device, vm.device_id)
        if dev is not None:
            return dev, "linked"
    if not vm.node:
        return None, None
    node = vm.node.lower()
    dev = (await session.execute(
        select(Device).where(func.lower(Device.name) == node).limit(1))).scalar_one_or_none()
    if dev is not None:
        return dev, "name"
    dev = (await session.execute(
        select(Device).where(func.lower(Device.fqdn) == node).limit(1))).scalar_one_or_none()
    if dev is not None:
        return dev, "fqdn"
    return None, None
