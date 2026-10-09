"""ARP 觀測的資料品質：哪些 MAC 不值得算進衝突與「頻繁換 MAC」（2026-10-09 一次 ARP 異常調查的回饋）。

兩種情況會讓同一個 IP 看起來有很多台機器，其實是上游資料的問題：

- **讀到一半的 MAC（corrupt）**：路由器的 ARP 項目正在兩張網卡之間切換時，SNMP 剛好讀到一半，
  LibreNMS 收進一個不存在的位址 —— 前幾個位元組來自 A、其餘來自 B（正式環境一次出現兩個，
  一正一反）。判準：**只有一個回報者**、**查不到廠商**，而且是同一個 IP 另外兩個 MAC 拼起來的，
  或跟一個有多個來源佐證的 MAC 只差一個位元組。
- **過期的快取（stale_cache）**：本地管理位址（虛擬網卡、bridge）只有一台設備的 ARP 表回報，
  而同一個 IP 的另一個 MAC 有兩個以上來源互相佐證。

這兩種都只是「降低可信度」：結果裡照樣列出、標上原因，讓人看得到；只是不算進衝突的機器數與
換過幾個 MAC。回報者＝一個資料來源加一台設備（LibreNMS 的每台設備各算一個；掃描代理、各防火牆
各算一個），同一個回報者看幾次都只算一次。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SUSPECT_CORRUPT = "corrupt"
SUSPECT_STALE = "stale_cache"


def is_locally_administered(mac: str) -> bool:
    """第一個位元組的 bit 1 為 1 ＝ 本地管理位址（虛擬網卡、容器、隨機化 MAC）。"""
    try:
        first = int(mac.replace(":", "").replace("-", "")[:2], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0b10)


def _octets(mac: str) -> list[str]:
    return mac.lower().split(":")


def is_splice(mac: str, others: Iterable[str]) -> bool:
    """`mac` 是不是另外兩個 MAC 的拼接：前 k 個位元組來自其中一個、其餘來自另一個（1 ≤ k ≤ 5）。"""
    b = _octets(mac)
    pool = [_octets(o) for o in others if o.lower() != mac.lower()]
    for head in pool:
        for tail in pool:
            if head == tail:
                continue
            for k in range(1, 6):
                if b[:k] == head[:k] and b[k:] == tail[k:]:
                    return True
    return False


def one_byte_apart(mac: str, others: Iterable[str]) -> bool:
    """跟另一個 MAC 只差一個位元組。"""
    b = _octets(mac)
    return any(sum(x != y for x, y in zip(b, _octets(o), strict=False)) == 1
               for o in others if o.lower() != mac.lower())


@dataclass
class MacSeen:
    """一個 IP 上的一個 MAC：誰回報過、什麼時候。"""

    first: datetime | None = None
    last: datetime | None = None
    sources: set[str] = field(default_factory=set)
    #: 回報者鍵 → {source, device_id, interface, last}
    reporters: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, reporter: str, *, source: str, at: datetime | None = None, first: datetime | None = None,
            device_id: Any = None, interface: str | None = None) -> None:
        self.sources.add(source)
        if at is not None and (self.last is None or at > self.last):
            self.last = at
        f = first or at
        if f is not None and (self.first is None or f < self.first):
            self.first = f
        cur = self.reporters.setdefault(reporter, {"source": source, "device_id": device_id,
                                                   "interface": interface, "last": None})
        if at is not None and (cur["last"] is None or at > cur["last"]):
            cur["last"] = at


def reporter_key(source: str, device_id: Any) -> str:
    """LibreNMS 的每台設備各算一個回報者；其他來源（掃描代理、各防火牆）各算一個。"""
    return f"{source}:{device_id}" if device_id else source


def classify_suspects(macs: dict[str, MacSeen], vendors: dict[str, str | None],
                     known: Iterable[str] = (), corroborated_known: Iterable[str] = ()) -> dict[str, str | None]:
    """同一個 IP 的所有 MAC → 每個 MAC 的可疑原因（None＝正常）。`vendors` 以完整 MAC 為鍵。

    `known`：真實存在的 MAC，不一定出現在這次的時間窗裡 —— 這台裝置的網卡、這個 IP 登記的 MAC、
    長時間有多個來源佐證過的。正式環境 2026-10-09：主機修好 arp_ignore 之後另一張網卡不再回應，
    最近一小時只剩真實 MAC 與路由器還在回報的拼接 MAC；沒有這份參考就認不出它是拼出來的。
    只拿來判斷「拼接」（兩個不同的 MAC 剛好拼成第三個，巧合的機率極低）。

    `corroborated_known`：長時間有多個來源佐證過的 MAC。「只差一個位元組」只跟這些比 ——
    登記的 MAC 不算：另一台機器的 MAC 剛好跟它只差一個位元組（同一批出廠、文件用範圍）時會誤判。
    """
    corroborated = {m for m, s in macs.items() if len(s.reporters) >= 2} | {k.lower() for k in corroborated_known}
    trusted = {k.lower() for k in known}
    out: dict[str, str | None] = {}
    for m, seen in macs.items():
        reason: str | None = None
        if len(seen.reporters) == 1:
            others = [o for o in macs if o != m] + [k for k in trusted if k != m and k not in macs]
            # 只差一個位元組：被「抄」的那個要有多個來源佐證 —— 兩台機器各只被看過一次、剛好只差
            # 一個位元組（測試資料、同一批出廠的未登記網卡）不能判成讀壞
            if not vendors.get(m) and (is_splice(m, others) or one_byte_apart(
                    m, [o for o in [*macs, *trusted] if o != m and o in corroborated])):
                reason = SUSPECT_CORRUPT
            elif is_locally_administered(m) and (corroborated - {m}):
                reason = SUSPECT_STALE
        out[m] = reason
    return out
