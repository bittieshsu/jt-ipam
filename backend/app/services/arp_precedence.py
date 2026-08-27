"""ARP / MAC 來源優先序。

多個來源（掃描代理 / LibreNMS / OPNsense / pfSense / FortiGate / Windows DHCP /
AdGuard / Proxmox / 手動）可能都替同一個 IP 回報 MAC。本模組決定誰能覆寫誰。

排序、停用、快取等共通機制在 `services/precedence.py`；這裡只留 MAC 特有的部分：
正規化與覆寫規則。每個來源的性質（會不會過期、屬於哪一層）登記在 `services/evidence.py`。
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import IPAddress
from app.services.precedence import Precedence

ARP_KEY = "arp_precedence"
ARP_SOURCES = ("manual", "scanner", "opnsense", "pfsense", "fortigate", "windows_dhcp",
               "librenms", "adguard", "proxmox")
# 預設：手動最優先，其次主動掃描、防火牆 ARP、LibreNMS、AdGuard、Proxmox
DEFAULT_ARP_ORDER: list[str] = list(ARP_SOURCES)

_P = Precedence(key=ARP_KEY, sources=ARP_SOURCES, default_order=tuple(DEFAULT_ARP_ORDER))


def _bust() -> None:
    _P.bust()


async def get_arp_precedence(session: AsyncSession) -> list[str]:
    return await _P.get_order(session)


async def get_arp_disabled(session: AsyncSession) -> list[str]:
    return await _P.get_disabled(session)


async def set_arp_precedence(
    session: AsyncSession, *, order: list[str],
    disabled: list[str] | None = None, updated_by_user_id: uuid.UUID | None = None,
) -> tuple[list[str], list[str]]:
    return await _P.save(session, order=order, disabled=disabled,
                         updated_by_user_id=updated_by_user_id)


def normalize_mac(v: object) -> str:
    """MAC 正規化成無分隔的小寫十六進位。

    比對前一定要兩邊都做：asyncpg 把 MACADDR 欄位回成**物件**，字串化後帶冒號
    （`bc:24:11:6a:58:ef`），而各來源送進來的多半是無分隔式（`bc24116a58ef`）。
    直接比字串永遠不相等 —— 實機上因此每輪同步都判定「MAC 變了」，
    24 小時寫出 990 筆假的異動記錄（同一個 IP 一天 90 次、新值卻始終相同）。
    """
    return "".join(c for c in str(v or "").lower() if c in "0123456789abcdef")


def _same_mac(a: object, b: object) -> bool:
    na, nb = normalize_mac(a), normalize_mac(b)
    return bool(na) and na == nb


async def consider_mac(
    session: AsyncSession, *, ip: IPAddress, mac: str | None, source: str,
) -> bool:
    """依優先序決定是否用此來源的 MAC 覆寫 ip.mac。回傳是否有更新。

    規則：
      - 沒 MAC（清空）→ 不動
      - ip 目前沒 MAC → 直接寫，記來源
      - ip 已有 MAC 但來源未知（legacy）→ 保留，不覆寫（避免蓋掉人工/舊資料）
      - ip 已有 MAC 且已知來源 → 只有新來源優先序更高（排名更小）才覆寫
    """
    if not mac:
        return False
    mac = mac.strip().lower()
    if source not in ARP_SOURCES:
        source = "scanner"
    order, disabled = await _P.load(session)
    if source in disabled:
        return False   # 該來源已停用 → 不參與 MAC 覆寫
    if ip.mac is None:
        ip.mac = mac
        ip.mac_source = source
        return True
    if ip.mac_source is None:
        return False

    new_rank = _P.rank(order, source)
    cur_rank = _P.rank(order, ip.mac_source)
    if new_rank < cur_rank or (new_rank == cur_rank and not _same_mac(ip.mac, mac)):
        ip.mac = mac
        ip.mac_source = source
        return True
    return False
