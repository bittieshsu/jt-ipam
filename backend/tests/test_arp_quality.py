"""ARP 觀測的資料品質（2026-10-09 一次 ARP 異常調查的回饋）。

正式環境實例（已換成通用位址）：路由器的 ARP 表正在兩張網卡的 MAC 之間切換時，SNMP 讀到一半，
LibreNMS 收進兩個不存在的 MAC：一個的第一個位元組來自 A、其餘來自 B，另一個反過來。
它們只有那一台路由器回報過、查不到廠商，卻讓這個 IP 被算成「4 個 MAC」。
另一種是本地管理位址的過期快取：只有一台交換器的 bridge 回報，其他六個來源看到的都是另一個 MAC。
"""
from __future__ import annotations

from app.services.arp_quality import (
    SUSPECT_CORRUPT,
    SUSPECT_STALE,
    MacSeen,
    classify_suspects,
    is_splice,
    one_byte_apart,
)

A = "02:00:5e:10:00:a1"     # 主機板網卡（測試用本地管理位址，不屬於任何廠商）
B = "06:00:5e:20:00:b2"     # 擴充網卡
SPLICE_1 = "02:00:5e:20:00:b2"   # A 的第一個位元組＋B 的其餘
SPLICE_2 = "06:00:5e:10:00:a1"   # B 的第一個位元組＋A 的其餘


def _seen(*reporters: str) -> MacSeen:
    s = MacSeen()
    for r in reporters:
        s.add(r, source="librenms")
    return s


def test_splice_of_two_other_macs_is_recognised() -> None:
    assert is_splice(SPLICE_1, [A, B])
    assert is_splice(SPLICE_2, [A, B])
    assert not is_splice("00:00:5e:00:53:99", [A, B])


def test_one_byte_apart() -> None:
    assert one_byte_apart(SPLICE_1, [B])
    assert not one_byte_apart(A, [B])


def test_spliced_mac_seen_by_one_router_is_suspect_corrupt() -> None:
    macs = {A: _seen("router", "sw1", "sw2", "scanner"), B: _seen("router", "scanner"),
            SPLICE_1: _seen("router"), SPLICE_2: _seen("router")}
    vendors = {A: "Super Micro", B: "Hewlett Packard", SPLICE_1: None, SPLICE_2: None}
    got = classify_suspects(macs, vendors)
    assert got == {A: None, B: None, SPLICE_1: SUSPECT_CORRUPT, SPLICE_2: SUSPECT_CORRUPT}


def test_a_known_vendor_or_a_second_reporter_is_not_corrupt() -> None:
    # 廠商查得到（例如同一張多埠網卡的相鄰位址）→ 不當成損壞
    macs = {A: _seen("router"), "02:00:5e:10:00:a2": _seen("router")}
    assert classify_suspects(macs, {A: "Super Micro", "02:00:5e:10:00:a2": "Super Micro"}) == {
        A: None, "02:00:5e:10:00:a2": None}
    # 兩個來源都看過 → 不是單一來源讀壞
    macs = {A: _seen("router", "sw1"), B: _seen("router", "sw1"), SPLICE_1: _seen("router", "sw1")}
    assert classify_suspects(macs, {A: "x", B: "y"})[SPLICE_1] is None


def test_single_source_local_mac_disagreeing_with_corroborated_mac_is_stale() -> None:
    real = "00:00:5e:00:53:10"
    local = "0e:00:5e:00:53:11"            # 本地管理位址
    macs = {real: _seen("sw1", "sw2", "router", "scanner"), local: _seen("sw3")}
    got = classify_suspects(macs, {real: "Proxmox", local: None})
    assert got == {real: None, local: SUSPECT_STALE}


def test_local_mac_alone_or_without_a_corroborated_rival_is_not_stale() -> None:
    local = "0e:00:5e:00:53:11"
    assert classify_suspects({local: _seen("sw3")}, {local: None}) == {local: None}
    other = "02:00:5e:00:53:12"
    # 對手也只有一個來源：沒有誰比較可信，不判
    assert classify_suspects({local: _seen("sw3"), other: _seen("sw4")}, {})[local] is None


def test_reporters_are_counted_once_each() -> None:
    s = MacSeen()
    s.add("router", source="librenms")
    s.add("router", source="librenms")
    s.add("scanner", source="scanner")
    assert len(s.reporters) == 2


def test_two_single_sighting_macs_one_byte_apart_are_not_corrupt() -> None:
    """兩台機器各只被看過一次、MAC 剛好只差一個位元組：沒有誰比較可信，不能判成讀壞。"""
    a, b = "00:00:5e:00:53:01", "00:00:5e:00:53:02"
    assert classify_suspects({a: _seen("scanner"), b: _seen("scanner")}, {}) == {a: None, b: None}



def test_splice_partner_outside_the_window_still_counts_when_known() -> None:
    """正式環境 2026-10-09：主機修好 arp_ignore 之後，另一張網卡的 MAC 不再出現在最近一小時，
    路由器卻還在回報拼出來的那個。被拼的對象要能從「已知的真實 MAC」（裝置網卡、登記的 MAC、
    長時間有多個來源佐證的）找到。"""
    macs = {A: _seen("router", "sw1", "scanner"), SPLICE_1: _seen("router")}
    # 沒有參考：看不出是拼出來的（測試用的是本地管理位址，所以會落到「過期快取」，但不會是讀壞）
    assert classify_suspects(macs, {A: "Super Micro"})[SPLICE_1] != SUSPECT_CORRUPT
    assert classify_suspects(macs, {A: "Super Micro"}, known={B})[SPLICE_1] == SUSPECT_CORRUPT
