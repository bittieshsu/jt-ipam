"""證據契約的守門。

這組測試守的是一件很具體的事：**新增一個來源時，必須先回答「它的證據會不會過期」**。

沒有這道門的代價已經付過了：ARP 被當成有時間概念的證據，讓一台關機好幾週的 VM
顯示「52 天全綠」（v0.5.206）。當時「哪些來源會過期」散在各處的字串比對裡
（`if "scanner" in status`），新增來源時沒有任何東西會提醒你。
"""
from __future__ import annotations

import pytest

from app.models.ip_hostname import HOSTNAME_SOURCES
from app.services import evidence
from app.services.arp_precedence import ARP_SOURCES
from app.services.device_name_precedence import DEVNAME_SOURCES
from app.services.model_precedence import MODEL_SOURCES
from app.services.os_precedence import OS_SOURCES
from app.services.precedence import Precedence


@pytest.mark.parametrize(
    ("label", "sources"),
    [
        ("hostname", HOSTNAME_SOURCES),
        ("arp", ARP_SOURCES),
        ("os", tuple(OS_SOURCES)),
        ("device_name", DEVNAME_SOURCES),
        ("model", MODEL_SOURCES),
    ],
)
def test_every_precedence_source_is_registered(label: str, sources: tuple[str, ...]) -> None:
    """任何優先序裡出現的來源，都必須在證據登記表裡宣告 tier 與 aging。"""
    missing = [s for s in sources if evidence.get_source(s) is None]
    assert missing == [], (
        f"{label} 有未登記的來源 {missing} —— 請到 services/evidence.py 宣告它的 tier 與 "
        "aging（這個來源的資料會不會過期）"
    )


def test_learned_sources_never_age() -> None:
    """`learned` 這一層講的是「曾經學到這個對應」，不是「現在活著」。

    ARP／FDB／DNS／DHCP／虛擬化設定都屬於這層：它們沒有時間概念，
    來源設備的快取不老化，機器關掉之後那筆記錄還在。
    """
    for src in evidence.SOURCES.values():
        if src.tier == evidence.TIER_LEARNED:
            assert src.aging is False, f"{src.name} 標成 learned 卻宣稱會過期"


def test_arp_specifically_does_not_age() -> None:
    """實機事故的回歸測試：ARP 不可以被當成會過期的存活證據。"""
    assert evidence.is_aging("arp") is False
    assert evidence.tier_of("arp") == evidence.TIER_LEARNED


def test_unregistered_source_is_treated_as_non_aging() -> None:
    """不認得的來源一律當成「不會過期」—— 不知道能撐多久，就不要拿來宣稱現在還活著。"""
    assert evidence.is_aging("some_new_integration") is False
    assert evidence.get_source("some_new_integration") is None


def test_aging_sources_are_the_ones_we_actually_probe() -> None:
    """會過期的來源＝我們主動探測或第三方監控在輪詢的那些。"""
    assert set(evidence.aging_sources()) == {"scanner", "librenms", "zabbix", "wazuh"}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("online (scanner)", "scanner"),
        ("online (librenms)", "librenms"),
        ("online (arp)", "arp"),
        ("online", None),
        ("offline", None),
        ("unknown", None),
        (None, None),
        ("online ()", None),
    ],
)
def test_source_from_status(status: str | None, expected: str | None) -> None:
    assert evidence.source_from_status(status) == expected


# ─────────────────── 共用的優先序機制 ───────────────────

_P = Precedence(key="test_precedence", sources=("manual", "alpha", "beta"),
                default_order=("manual", "alpha", "beta"))


def test_sanitize_fills_in_missing_sources() -> None:
    """舊設定沒列到的新來源要補回去 —— 少了這步，新來源會安靜地整個消失。"""
    assert _P.sanitize_order(["beta"]) == ["beta", "manual", "alpha"]
    assert _P.sanitize_order("不是清單") == ["manual", "alpha", "beta"]
    assert _P.sanitize_order(["beta", "beta", "不存在的來源"]) == ["beta", "manual", "alpha"]


def test_protected_source_cannot_be_disabled() -> None:
    """至少要留一條人工可以蓋過去的路。"""
    assert _P.sanitize_disabled(["manual", "alpha"]) == ["alpha"]


def test_pick_follows_order_and_skips_disabled() -> None:
    order = ["manual", "alpha", "beta"]
    assert _P.pick({"alpha": "A", "beta": "B"}, order) == ("alpha", "A")
    assert _P.pick({"alpha": "A", "beta": "B"}, order, ["alpha"]) == ("beta", "B")
    # 空字串與 None 都不算有值
    assert _P.pick({"alpha": "  ", "beta": "B"}, order) == ("beta", "B")
    assert _P.pick({"alpha": None, "beta": "B"}, order) == ("beta", "B")
    assert _P.pick({}, order) == (None, None)


def test_rank_puts_unknown_sources_last() -> None:
    order = ["manual", "alpha"]
    assert _P.rank(order, "manual") < _P.rank(order, "alpha")
    assert _P.rank(order, "alpha") < _P.rank(order, "beta")
    assert _P.rank(order, None) >= _P.rank(order, "beta")
