"""上線判定：逐來源的證據（`arp_seen`）與 Wazuh。

## 這組測試在守什麼

在這之前，四家防火牆同步回來的 **ARP 表、DHCP 租約、VPN 連線**全部寫進
`ip_addresses.last_seen_scanner`。兩個後果：

1. 畫面顯示「上線 (scanner)」，但站台根本沒有掃描代理 —— 來源是假的。
2. 管理員沒辦法只採信其中一部分，儘管三者的可信度差很多。

拆開之後最容易踩到的回歸是**方向相反的兩種**，兩邊都測：

- 拆太乾淨 → 只被防火牆看到的 IP 一夜之間全變離線、還被報成「失聯 IP」
- 拆得不夠 → DHCP 租約（可能是三天前拿的）繼續被當成「現在活著」
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import arp_seen as arp_seen_svc
from app.services import evidence
from app.services.uptime import carry_forward_ok


class _IP:
    """IPAddress 的最小替身 —— 這些函式只讀屬性。"""

    def __init__(self, **kw):
        self.arp_seen = {}
        self.last_seen_scanner = None
        self.last_seen_librenms = None
        self.last_seen_wazuh = None
        self.__dict__.update(kw)


# ─────────────────── stamp 的兩個坑 ───────────────────

def test_stamp_replaces_the_dict_so_jsonb_gets_written() -> None:
    """就地改 dict 不會被 SQLAlchemy 存下來 —— 一定要換成新的物件。

    這個 bug 的症狀是「同步看起來成功、資料庫裡什麼都沒有」，非常難查。
    """
    ip = _IP()
    before = ip.arp_seen
    arp_seen_svc.stamp(ip, "arp:opnsense")
    assert ip.arp_seen is not before, "就地改 JSONB：SQLAlchemy 不會發現，資料不會寫回"
    assert "arp:opnsense" in ip.arp_seen


def test_permanent_arp_entries_are_never_stamped() -> None:
    """靜態／永久 ARP 項目不會因為機器關機而消失 → 拿它宣稱上線＝永遠綠燈。"""
    ip = _IP()
    arp_seen_svc.stamp(ip, "arp:opnsense", permanent=True)
    assert ip.arp_seen == {}


def test_newest_aging_ignores_leases() -> None:
    """DHCP 租約撐好幾天，不能算「現在活著」；ARP 表會逾時淘汰，可以。"""
    now = datetime.now(UTC)
    ip = _IP()
    arp_seen_svc.stamp(ip, "lease:pfsense", now)
    arp_seen_svc.stamp(ip, "arp:pfsense", now - timedelta(minutes=5))
    ts, src = arp_seen_svc.newest_aging(ip, {"lease:pfsense", "arp:pfsense"})
    assert src == "arp:pfsense", "較新的 lease 不該勝出 —— 它沒有資格宣稱上線"
    assert ts is not None


def test_newest_only_counts_selected_sources() -> None:
    """沒被勾選的來源不算數（設定就是設定，不是建議）。"""
    ip = _IP()
    arp_seen_svc.stamp(ip, "arp:fortigate")
    assert arp_seen_svc.newest(ip, {"arp:opnsense"}) == (None, None)
    assert arp_seen_svc.newest(ip, {"arp:fortigate"})[1] == "arp:fortigate"


def test_garbage_timestamps_do_not_explode() -> None:
    """外來 JSONB 可能有壞資料（手改、舊格式）—— 略過那一筆，不是整個炸掉。"""
    ip = _IP(arp_seen={"arp:opnsense": "not-a-time", "arp:pfsense": None})
    assert arp_seen_svc.newest(ip) == (None, None)


# ─────────────────── 預設值：不能改變既有站台的行為 ───────────────────

def test_firewall_arp_is_trusted_by_default() -> None:
    """回歸：防火牆的 ARP 原本被算成 scanner（預設採信）。拆開之後如果不預設採信，
    只接防火牆、沒有掃描代理的站台會整批變離線 —— 那是升級造成的災難，不是修正。"""
    defaults = evidence.default_liveness_sources()
    for vendor in ("opnsense", "pfsense", "fortigate", "paloalto"):
        assert f"arp:{vendor}" in defaults
    assert "wazuh" in defaults, "使用者明確要求把 Wazuh agent 納入上線判定"


def test_leases_and_librenms_arp_are_not_trusted_by_default() -> None:
    defaults = evidence.default_liveness_sources()
    for key in ("arp", "arp:librenms", "lease:opnsense", "lease:pfsense",
                "lease:fortigate", "lease:paloalto"):
        assert key not in defaults


# ─────────────────── 可用性長條圖的延續規則 ───────────────────

@pytest.mark.parametrize(
    ("status", "present", "expected"),
    [
        # 宣稱的來源現在還在 → 可以往後延續
        ("online (arp:opnsense)", {"arp:opnsense"}, True),
        # 那台防火牆不再看得到它了 → 不可以拿舊記錄畫成一路都通
        ("online (arp:opnsense)", set(), False),
        # 換了一家防火牆也不行 —— 來源要對得上
        ("online (arp:opnsense)", {"arp:pfsense"}, False),
        # 不會過期的證據一律不得延續（52 天全綠事故的成因）
        ("online (arp)", {"arp:opnsense"}, False),
    ],
)
def test_carry_forward_follows_the_claimed_source(
    status: str, present: set[str], expected: bool,
) -> None:
    assert carry_forward_ok(
        status, has_scanner=False, has_librenms=False, present=present) is expected


# ─────────────────── recompute_effective_status（實際跑一次）───────────────────

async def _mk_ip(session, ip_text: str, **kw):
    import uuid

    from app.models.address import IPAddress
    from app.models.section import Section
    from app.models.subnet import Subnet

    sec = Section(name=f"lv-{uuid.uuid4().hex[:6]}")
    session.add(sec)
    await session.flush()
    sub = Subnet(cidr="10.91.0.0/24", section_id=sec.id)
    session.add(sub)
    await session.flush()
    ipa = IPAddress(subnet_id=sub.id, ip=ip_text, **kw)
    session.add(ipa)
    await session.flush()
    return ipa


async def _recompute(session, sources: list[str]) -> None:
    """跑一次重算，來源清單直接指定（不動系統設定表）。"""
    from unittest.mock import patch

    from app.services import librenms as lnms

    async def _cfg(_s):
        return {"minutes": 30, "sources": sources}

    with patch.object(lnms, "get_liveness_config", _cfg, create=True), \
         patch("app.services.system_config.get_liveness_config", _cfg):
        await lnms.recompute_effective_status(session, instance=None)  # type: ignore[arg-type]


async def test_firewall_arp_makes_it_online_with_the_real_source_name(db_session):
    """回歸：這裡以前會顯示 `online (scanner)` —— 站台根本沒有掃描代理。"""
    now = datetime.now(UTC)
    ipa = await _mk_ip(db_session, "10.91.0.11")
    arp_seen_svc.stamp(ipa, "arp:opnsense", now)
    await db_session.commit()

    await _recompute(db_session, ["scanner", "librenms", "arp:opnsense"])
    await db_session.refresh(ipa)
    assert ipa.effective_status == "online (arp:opnsense)"


async def test_wazuh_keepalive_makes_it_online(db_session):
    """使用者要求：Wazuh agent 的 keep-alive 要算上線。"""
    ipa = await _mk_ip(db_session, "10.91.0.12",
                       last_seen_wazuh=datetime.now(UTC) - timedelta(minutes=2))
    await db_session.commit()

    await _recompute(db_session, ["scanner", "librenms", "wazuh"])
    await db_session.refresh(ipa)
    assert ipa.effective_status == "online (wazuh)"

    # 沒勾就不算 —— 設定要真的有效。
    # 這時候是「未知」而不是「離線」：被採信的來源從來沒看過它，我們就沒有立場
    # 說它掛了（與 scanner／LibreNMS 沒被勾選時的行為一致）。
    await _recompute(db_session, ["scanner", "librenms"])
    await db_session.refresh(ipa)
    assert ipa.effective_status == "unknown"


async def test_dhcp_lease_alone_is_not_online(db_session):
    """租約可能是三天前拿的：不能因為有租約就說機器現在活著。"""
    ipa = await _mk_ip(db_session, "10.91.0.13")
    arp_seen_svc.stamp(ipa, "lease:pfsense", datetime.now(UTC))
    await db_session.commit()

    await _recompute(db_session, evidence.default_liveness_sources())
    await db_session.refresh(ipa)
    assert ipa.effective_status != "online"
    assert not (ipa.effective_status or "").startswith("online")


async def test_stale_firewall_arp_becomes_offline(db_session):
    """防火牆的 ARP 表會逾時淘汰 —— 過了門檻就是離線，不是永遠上線。"""
    ipa = await _mk_ip(db_session, "10.91.0.14")
    arp_seen_svc.stamp(ipa, "arp:fortigate", datetime.now(UTC) - timedelta(hours=6))
    await db_session.commit()

    await _recompute(db_session, evidence.default_liveness_sources())
    await db_session.refresh(ipa)
    assert ipa.effective_status == "offline"


# ─────────────────── ARP 條目自己帶的時間 ───────────────────

def test_expiry_is_converted_back_to_when_it_was_refreshed() -> None:
    """「還在 ARP 表裡」不等於「剛剛才看到」。

    實機（兩台 OPNsense）每一筆都帶 `expires`：從 1200 秒往下數。
    一筆 `expires=343` 代表這個對應是 **14 分鐘前**更新的 —— 蓋上同步當下的時間，
    等於把快過期的條目講成剛看到，那正是我們批評 LibreNMS ARP 的那個毛病。
    """
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    fresh = arp_seen_svc.seen_from_remaining(1190, 1200, now=now)
    stale = arp_seen_svc.seen_from_remaining(343, 1200, now=now)
    assert fresh == now - timedelta(seconds=10)
    assert stale == now - timedelta(seconds=857)
    # 30 分鐘的門檻下，這兩筆都還算上線；但差了 14 分鐘 —— 到期時間會跟著差 14 分鐘
    assert (fresh - stale) == timedelta(seconds=847)


def test_permanent_entry_gives_no_time_and_is_skipped() -> None:
    """實機上 `permanent=True` 的那筆 `expires` 是 -1（防火牆自己的介面）。"""
    assert arp_seen_svc.seen_from_remaining(-1, 1200) is None
    ip = _IP()
    arp_seen_svc.stamp(ip, "arp:opnsense", permanent=True)
    assert ip.arp_seen == {}


def test_age_style_is_converted_too() -> None:
    """FortiOS 的 ARP 給的是「已經過幾秒」，方向相反但意思一樣。"""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    assert arp_seen_svc.seen_from_age(120, now=now) == now - timedelta(minutes=2)
    assert arp_seen_svc.seen_from_age("not-a-number") is None
    assert arp_seen_svc.seen_from_age(999999) is None      # 一天以上的年齡不合理


def test_unusable_values_fall_back_to_the_caller() -> None:
    """給不出時間時回 None，由呼叫端決定退回同步當下 —— 不要自己編一個時間出來。"""
    assert arp_seen_svc.seen_from_remaining(None, 1200) is None
    assert arp_seen_svc.seen_from_remaining(5000, 1200) is None   # 比 max_age 還大
    assert arp_seen_svc.seen_from_remaining(600, 0) is None
