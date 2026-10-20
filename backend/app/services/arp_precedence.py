"""ARP / MAC 來源優先序。

多個來源（掃描代理 / LibreNMS / OPNsense / pfSense / FortiGate / Windows DHCP /
AdGuard / Proxmox / 手動）可能都替同一個 IP 回報 MAC。本模組決定誰能覆寫誰。

排序、停用、快取等共通機制在 `services/precedence.py`；這裡只留 MAC 特有的部分：
正規化與覆寫規則。每個來源的性質（會不會過期、屬於哪一層）登記在 `services/evidence.py`。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import IPAddress
from app.services.precedence import Precedence

ARP_KEY = "arp_precedence"
ARP_SOURCES = ("manual", "scanner", "opnsense", "pfsense", "fortigate", "paloalto", "checkpoint", "mikrotik",
               # SNMP 收集（0217）：路由器／L3 交換器的 ARP 表，跟防火牆的 ARP 表同一級
               "snmp",
               "windows_dhcp", "kea_dhcp", "isc_dhcp", "isoinsight", "technitium", "librenms", "adguard", "proxmox")
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
    latest: dict[Any, Any] | None = None,
) -> bool:
    """依優先序決定是否用此來源的 MAC 覆寫 ip.mac。回傳是否有更新。

    規則：
      - 沒 MAC（清空）→ 不動
      - ip 目前沒 MAC → 直接寫，記來源
      - ip 已有 MAC 但來源未知（舊資料、匯入）→ 優先序最低，任何來源都可以覆寫。以前是「保留」，結果 DHCP 位址
        換成另一台之後，掃描、防火牆 ARP、租約都看到新的 MAC，IP 記錄卻永遠停在上一台（2026-10-05 正式環境卡住
        30 個）。人工編輯的 MAC 一律標記 manual（優先序最高），不受影響
      - ip 已有 MAC 且已知來源 → 只有新來源優先序更高（排名更小）才覆寫
    """
    if not mac:
        return False
    mac = mac.strip().lower()
    if source not in ARP_SOURCES:
        source = "scanner"
    order, disabled = await _P.load(session)
    if would_take(order, disabled, cur_mac=ip.mac, cur_source=ip.mac_source, mac=mac, source=source):
        return await _apply(session, ip=ip, mac=mac, source=source, latest=latest)
    return False


def pick_mac(cur_mac: object, counts: dict[str, int]) -> str | None:
    """同一個來源這一輪對同一個 IP 回報的 MAC（正規化後 → 幾筆回報）→ 要套用哪一個；None＝不動。

    取回報最多的；目前的 MAC 也在並列最多之中就留著目前的；並列最多的有好幾個、又都不是目前的 → 不猜。
    同一個 IP 報出好幾個 MAC 的情況：兩台 VM 設了同一個 IP、某台設備的 ARP 快取沒老化還留著上一台、
    防火牆的 ARP 是新的那台而租約還是上一台。以前照回報順序一筆一筆套，同一輪、每一輪都來回換
    （2026-10-05 正式環境 15 個 IP），而且換設備會清名稱，名稱也跟著每輪被清。
    """
    if not counts:
        return None
    top = max(counts.values())
    best = [m for m, n in counts.items() if n == top]
    cur = normalize_mac(cur_mac) if cur_mac else None
    if cur in best:
        return cur
    return best[0] if len(best) == 1 else None


class MacRun:
    """一個來源的一輪同步裡對 MAC 的回報：收齊之後每個 IP 只決定一次（pick_mac），不隨回報順序來回換。"""

    def __init__(self, session: AsyncSession, *, source: str) -> None:
        self.session = session
        self.source = source
        self._seen: dict[Any, tuple[IPAddress, dict[str, list[Any]]]] = {}

    def report(self, ip: IPAddress, mac: str | None) -> None:
        n = normalize_mac(mac)
        if not n:
            return
        key = ip.id if ip.id is not None else id(ip)
        self._seen.setdefault(key, (ip, {}))[1].setdefault(n, [0, str(mac).strip().lower()])[0] += 1

    async def finish(self) -> int:
        """套用並回傳 MAC 有更新的 IP 數。"""
        changed = 0
        seen, self._seen = self._seen, {}
        for ip, macs in seen.values():
            pick = pick_mac(ip.mac, {k: v[0] for k, v in macs.items()})
            if pick is not None and await consider_mac(self.session, ip=ip, mac=macs[pick][1], source=self.source):
                changed += 1
        return changed


async def load_precedence(session: AsyncSession) -> tuple[Any, Any]:
    """(優先序, 停用的來源)：給整批處理的呼叫端先取一次，再逐筆用 would_take() 判斷。"""
    return await _P.load(session)


def would_take(order: Any, disabled: Any, *, cur_mac: object, cur_source: str | None,
               mac: str | None, source: str) -> bool:
    """這個來源的 MAC 要不要覆寫 IP 目前的 MAC（consider_mac 與整批同步共用同一份規則）。"""
    if not mac:
        return False
    hexs = normalize_mac(mac)
    # 全零、廣播、只有第一個位元組的殘缺值（LibreNMS 偶爾給 c4:00:00:00:00:00）不是任何一台機器
    if len(hexs) != 12 or hexs in ("000000000000", "ffffffffffff") or hexs[2:] == "0000000000":
        return False
    if source not in ARP_SOURCES:
        source = "scanner"
    if source in disabled:
        return False   # 該來源已停用 → 不參與 MAC 覆寫
    if cur_mac is None:
        return True
    if cur_source is None:
        return True    # 來源不明（舊資料、匯入）→ 最低優先；人工編輯的都標成 manual
    new_rank = _P.rank(order, source)
    cur_rank = _P.rank(order, cur_source)
    return new_rank < cur_rank or (new_rank == cur_rank and not _same_mac(cur_mac, mac))


async def is_other_nic(session: AsyncSession, *, ip: IPAddress, mac: str) -> bool:
    """`mac` 是不是同一台主機另一張網卡的（ARP flux），不該寫成這個 IP 的 MAC。

    正式環境 2026-10-20：雙網卡主機兩個網段在同一個二層（Linux 預設 arp_ignore=0），兩張網卡都回答這個 IP，
    看到哪張就寫哪張 → 一個 IP 30 天換了 91 次 MAC。全部成立才算：
      - 新的 MAC 登記在**另一個子網路**的 IP 上（那張網卡自己的位址；同子網路共用 MAC 是別名／VIP，不算）
      - 目前的 MAC 只屬於這個 IP（已經被寫錯、兩筆掛同一個 MAC 時要能換回來）
      - 目前的 MAC 24 小時內還在這個 IP 被看到（真的換網卡、搬 VLAN 時舊的不會再出現）
    """
    from datetime import UTC, datetime

    from sqlalchemy import Text, cast, select

    from app.models.librenms import ARPEntry
    if ip.mac is None or _same_mac(ip.mac, mac):
        return False

    def colon(v: object) -> str:
        h = normalize_mac(v)
        return ":".join(h[i:i + 2] for i in range(0, 12, 2))
    # MACADDR 欄位有索引，等號比對（寫法不同也比得到）；大站台每輪可能有上百次換 MAC，不可逐列算字串
    elsewhere = (await session.execute(select(IPAddress.subnet_id, cast(IPAddress.mac, Text)).where(
        IPAddress.id != ip.id, IPAddress.mac.in_([colon(mac), colon(ip.mac)])))).all()  # bounded: 兩個 MAC
    new_here = [sid for sid, m in elsewhere if _same_mac(m, mac)]
    if not new_here or all(sid == ip.subnet_id for sid in new_here):
        return False
    if any(_same_mac(m, ip.mac) for _sid, m in elsewhere):
        return False
    since = datetime.now(UTC) - timedelta(hours=24)
    return (await session.execute(select(ARPEntry.id).where(
        ARPEntry.ip == str(ip.ip).split("/")[0], ARPEntry.mac == colon(ip.mac),
        ARPEntry.last_seen_at >= since).limit(1))).first() is not None


async def _apply(
    session: AsyncSession, *, ip: IPAddress, mac: str, source: str,
    latest: dict[Any, Any] | None = None,
) -> bool:
    """覆寫 ip.mac，**並留下異動記錄**。

    主機名稱每次改變都會寫一筆，MAC 卻不會 —— 於是 IP 詳細資料頁的時間軸上
    看得到「主機名稱變更」，卻永遠看不到「這台什麼時候換了網卡」，即使 ARP 表裡
    看得出來。同一個值再看到一次不寫（每輪同步都會看到，寫了會把時間軸洗掉）。
    """
    from app.services.ip_history import had_mac_recently, log_change

    old_mac = ip.mac
    if _same_mac(old_mac, mac):
        # 值本來就對、只是來源不明：記下來源，之後照正常優先序（不寫異動記錄）
        if ip.mac_source is None:
            ip.mac_source = source
        return False
    if await is_other_nic(session, ip=ip, mac=mac):
        return False
    # 換回 24 小時內用過的 MAC＝同一批設備輪流出現（雙網卡的 ARP flux、兩台 VM 同一個 IP），不是換了一台設備
    flap_back = old_mac is not None and await had_mac_recently(
        session, ip=ip, mac=mac, within=timedelta(hours=24))
    ip.mac = mac
    ip.mac_source = source
    await log_change(
        session, ip=ip, event_type="mac_changed", field="mac",
        old=str(old_mac) if old_mac is not None else None, new=str(mac),
        source=source, latest=latest,
    )
    # 換了一台設備（不是第一次填 MAC、也不是來回跳）：上一台的辨識結果、OCS 盤點、自己報的名稱都不再適用
    if old_mac is not None and not flap_back:
        from app.services.device_turnover import on_device_replaced
        await on_device_replaced(session, ip=ip, source=source)
    return True
