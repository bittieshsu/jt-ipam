"""防火牆給的逐來源觀測時間（`ip_addresses.arp_seen`）。

## 為什麼有這個檔案

在這之前，OPNsense／pfSense／FortiGate／Palo Alto 同步回來的 ARP 表、DHCP 租約與
VPN 連線，**全部**都寫進 `ip_addresses.last_seen_scanner`。結果有兩個：

1. 畫面上出現「上線 (scanner)」，但那台站台根本沒有裝掃描代理 —— 來源是騙人的。
2. 管理員沒辦法只採信其中一部分。三種證據的可信度差很多（ARP 表按逾時淘汰、
   VPN 連線是此刻連著、DHCP 租約可能是三天前拿的），混在同一個欄位就分不開。

所以改成逐來源記時間，鍵是證據契約裡登記的名字（`arp:opnsense`、`vpn:pfsense`、
`lease:fortigate`…），能不能拿來宣稱上線由 `services/evidence.py` 決定。

## 兩個實作上的坑

- **JSONB 就地改不會被存下來**：SQLAlchemy 預設不追蹤 dict 內容變動，
  `ipa.arp_seen["x"] = ...` 會安靜地不寫回資料庫。這裡一律建新 dict 再指派。
- **靜態／永久 ARP 項目不會淘汰**：那種項目留在表裡跟機器活不活著無關，
  拿它宣稱上線就是重演「關機數週卻全綠」那個事故。所以 `permanent=True` 直接不記。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.services.evidence import is_aging


def stamp(
    ipa: Any, source: str, when: datetime | None = None, *, permanent: bool = False,
) -> None:
    """把 `source` 的觀測時間記到這筆 IP 上。

    `source` 用完整的契約名稱（如 `arp:opnsense`）。`permanent=True`（靜態 ARP 項目）
    一律略過 —— 那種項目不會因為機器關機而消失。
    """
    if permanent or not source:
        return
    ts = (when or datetime.now(UTC)).astimezone(UTC)
    current = dict(getattr(ipa, "arp_seen", None) or {})
    current[source] = ts.isoformat()
    ipa.arp_seen = current      # 指派而非就地改：JSONB 才會被寫回


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def newest(
    ipa: Any, sources: set[str] | frozenset[str] | None = None,
) -> tuple[datetime | None, str | None]:
    """在被採信的來源之中，最近一次觀測的時間與來源名稱。

    `sources=None`＝不過濾（用於「這筆 IP 有哪些證據」的顯示）。
    """
    best_ts: datetime | None = None
    best_src: str | None = None
    for key, raw in (getattr(ipa, "arp_seen", None) or {}).items():
        if sources is not None and key not in sources:
            continue
        ts = _parse(raw)
        if ts is None:
            continue
        if best_ts is None or ts > best_ts:
            best_ts, best_src = ts, key
    return best_ts, best_src


def newest_aging(
    ipa: Any, sources: set[str] | frozenset[str],
) -> tuple[datetime | None, str | None]:
    """同上，但只看**會過期**的來源 —— 只有那些能宣稱「現在上線」。"""
    return newest(ipa, {s for s in sources if is_aging(s)})
