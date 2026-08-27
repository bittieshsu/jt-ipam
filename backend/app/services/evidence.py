"""證據契約：每個「來源」在說什麼，以及它的話能不能撐到明天。

## 為什麼需要這個檔案

jt-ipam 的每一個屬性（主機名稱、MAC、OS、裝置名稱、型號、存活狀態）都可能同時被
好幾個來源回報，而它們的**性質差很多**：

- 人工填的不會過期，但也不會自己更新
- 掃描代理探測到的有明確時間，過了就該失效
- **ARP／FDB 學到的沒有時間概念** —— LibreNMS 的 ARP API 連時間欄位都不回，
  我們只能因為「這筆還在清單裡」就蓋上同步當下的時鐘

第三點的代價是實際發生過的：一台關機好幾週的 VM 顯示「52 天全綠」，因為某台 AP 的
ARP 快取一直留著那筆記錄（v0.5.206/0.5.207 修）。當時的問題不是少了某個功能，而是
**「這個來源的證據會不會過期」這件事沒有任何地方寫下來**，散在各個模組的字串比對裡
（`if "scanner" in status`）。新增一個來源時，沒有任何東西會提醒你回答這個問題。

所以這裡把它變成契約：**每個來源都必須登記 tier 與 aging**，
未登記的來源一律視為「不會過期」（保守：不知道能撐多久，就不要拿它宣稱現在還活著）。

## tier 的意思

| tier | 意思 | 例 |
|---|---|---|
| `asserted` | 人宣告的意圖，不是觀測 | manual |
| `probed` | 我們主動去探、當場得到回應 | scanner |
| `monitored` | 第三方監控系統回報的裝置狀態 | librenms、zabbix、wazuh |
| `learned` | 被動學到的對應關係，**不代表現在還成立** | arp、fdb、dns、dhcp、虛擬化平台的設定 |

`learned` 一律 `aging=False`：它們回答的是「這個對應曾經被學到」，不是「現在活著」。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 人宣告的意圖（不是觀測結果）
TIER_ASSERTED = "asserted"
#: 我們主動探測、當場得到回應
TIER_PROBED = "probed"
#: 第三方監控系統回報的裝置狀態
TIER_MONITORED = "monitored"
#: 被動學到的對應關係 —— 不代表現在還成立
TIER_LEARNED = "learned"


@dataclass(frozen=True)
class Source:
    """一個證據來源的契約。

    `aging`：這個來源給的資料有沒有「時間概念」——
    也就是「超過一段時間沒再回報，就該視為失效」這件事成不成立。
    只有 `aging=True` 的來源可以用來宣稱「現在是上線的」。
    """

    name: str
    tier: str
    aging: bool


def _s(name: str, tier: str, aging: bool) -> Source:
    return Source(name=name, tier=tier, aging=aging)


#: 全站來源登記表。新增整合時**必須**在這裡登記，否則守門測試會擋下來。
SOURCES: dict[str, Source] = {s.name: s for s in (
    # 人工
    _s("manual", TIER_ASSERTED, aging=False),

    # 主動探測：掃描代理實際打封包，有明確時間
    _s("scanner", TIER_PROBED, aging=True),

    # 第三方監控：裝置狀態由對方輪詢維護，會過期
    _s("librenms", TIER_MONITORED, aging=True),
    _s("zabbix", TIER_MONITORED, aging=True),
    _s("wazuh", TIER_MONITORED, aging=True),

    # 被動學到的對應：不代表現在還活著
    # ARP 是這裡最容易誤用的一個 —— LibreNMS 的 ARP API 不回時間，
    # 來源設備（AP／路由器）的快取不老化，關機的機器也會一直看起來剛出現。
    _s("arp", TIER_LEARNED, aging=False),
    _s("fdb", TIER_LEARNED, aging=False),
    _s("dns", TIER_LEARNED, aging=False),
    _s("netbios", TIER_LEARNED, aging=False),
    _s("mdns", TIER_LEARNED, aging=False),
    _s("snmp", TIER_LEARNED, aging=False),

    # 防火牆／DHCP：回報的是設定與租約，不是活性
    _s("opnsense", TIER_LEARNED, aging=False),
    _s("pfsense", TIER_LEARNED, aging=False),
    _s("fortigate", TIER_LEARNED, aging=False),
    _s("windows_dhcp", TIER_LEARNED, aging=False),
    _s("adguard", TIER_LEARNED, aging=False),

    # 虛擬化平台：回報的是「設定上這台 VM 有這個 IP」
    _s("proxmox", TIER_LEARNED, aging=False),
    _s("esxi", TIER_LEARNED, aging=False),

    # 匯入來源
    _s("phpipam", TIER_ASSERTED, aging=False),
)}


#: 實際會餵進上線判定的來源（在 ip_addresses 上有對應的 last_seen_* 欄位）。
#: 這是「有登記」與「真的有資料進來」的交集 —— 不要把只登記、沒接線的來源列進設定頁。
LIVENESS_SOURCES: tuple[str, ...] = ("scanner", "librenms", "arp")


def default_liveness_sources() -> list[str]:
    """預設採信哪些：會過期的才算數。ARP 因此自動落在預設之外，不必另外寫死清單。"""
    return [s for s in LIVENESS_SOURCES if is_aging(s)]


def get_source(name: str | None) -> Source | None:
    return SOURCES.get((name or "").strip().lower())


def is_aging(name: str | None) -> bool:
    """這個來源的證據會不會過期。**未登記者一律視為不會過期**（保守）。"""
    src = get_source(name)
    return bool(src and src.aging)


def tier_of(name: str | None) -> str:
    src = get_source(name)
    return src.tier if src else TIER_LEARNED


def aging_sources() -> list[str]:
    """可以用來宣稱「現在上線」的來源。"""
    return sorted(s.name for s in SOURCES.values() if s.aging)


#: `effective_status` 長成 `online (scanner)` 這種；把括號裡的來源取出來。
def source_from_status(status: str | None) -> str | None:
    """從狀態字串取出來源後綴，取不到回 None（例如純 `online` / `offline`）。"""
    if not status:
        return None
    text = status.strip().lower()
    start = text.find("(")
    end = text.find(")", start + 1)
    if start < 0 or end < 0:
        return None
    inner = text[start + 1:end].strip()
    return inner or None
