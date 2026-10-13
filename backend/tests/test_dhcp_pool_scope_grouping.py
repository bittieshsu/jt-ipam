"""DHCP 集區使用率以「設定的範圍」為單位，不是扣掉排除後切出來的每一段（客戶 2026-10-12，升 1.0.7 後）。

客戶的 Windows DHCP scope 是 .100–.230，另外排除 7 個單一位址；Windows 自己的統計是總數 124、在用 74。
1.0.7 起範圍寫入前先扣掉排除區間（0207），一個 scope 因此被切成 8 段、各存一筆，而使用率與「集區快用完」
通知把每一段都當成一個集區：Windows 從頭依序發放，第一段 .100–.160 全被租走，就報「61/61（100%）」，
看起來像是範圍被縮成 .100–.160。

另一個問題：通知的識別鍵用的是範圍那一筆的 id，而每個 DHCP 來源每次同步都刪掉重建範圍 → id 每輪都變，
滿著的集區每一輪都被當成新的再通知一次（截圖裡同一則 2 分鐘前、8 分鐘前各一次）。

- 寫入端另外記下扣掉排除之前的原始範圍（pool_start／pool_end）
- 使用率依原始範圍合併：總數＝各段加總（已扣排除）、已用＝各段裡的 IP 記錄加總；顯示整個範圍與排除了幾個
- 通知的識別鍵用來源＋原始範圍，範圍重建也是同一個集區
"""
from __future__ import annotations

import uuid

import pytest
from app.models.address import IPAddress
from app.models.dhcp import DHCPPoolRange
from app.models.section import Section
from app.models.subnet import Subnet
from sqlalchemy import select

pytestmark = pytest.mark.anyio

NET = "198.51.100"
EXCLUDED = (161, 175, 188, 199, 204, 210, 222)


class _Win:
    def __init__(self, scopes, exclusions):
        self.scopes, self.ex = scopes, exclusions

    def get_scopes(self):
        return self.scopes

    def get_exclusions(self, sid):
        return self.ex.get(sid, [])


SCOPE = {"ScopeId": f"{NET}.0", "SubnetMask": "255.255.255.0", "StartRange": f"{NET}.100",
         "EndRange": f"{NET}.230", "Name": "LAN", "State": "Active"}
EXCL = {f"{NET}.0": [{"StartRange": f"{NET}.{n}", "EndRange": f"{NET}.{n}"} for n in EXCLUDED]}


async def _subnet(db_session) -> Subnet:  # type: ignore[no-untyped-def]
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr=f"{NET}.0/24")
    db_session.add(sub)
    await db_session.flush()
    return sub


async def _windows(db_session, monkeypatch):  # type: ignore[no-untyped-def]
    from app.models.windows_dhcp import WindowsDhcpServer
    from app.services import windows_dhcp as wd
    w = WindowsDhcpServer(name="dhcp-01", host="dhcp.corp.example.com",
                          username="CORP\\svc", password_enc=b"x", password_nonce=b"x")
    db_session.add(w)
    await db_session.flush()
    monkeypatch.setattr(wd, "_client", lambda _i: _Win([SCOPE], EXCL))
    await wd.sync_scopes(db_session, w)
    await db_session.flush()
    return w


async def _leased_like_windows(db_session, sub, n: int) -> None:  # type: ignore[no-untyped-def]
    """Windows 從範圍開頭依序發放（跳過排除的）：前 n 個可發放的位址都有 IP 記錄。"""
    last = 100
    while n:
        if last not in EXCLUDED:
            db_session.add(IPAddress(subnet_id=sub.id, ip=f"{NET}.{last}", in_dhcp_lease=True))
            n -= 1
        last += 1
    await db_session.flush()


async def test_writers_remember_the_configured_range(db_session, monkeypatch) -> None:
    w = await _windows(db_session, monkeypatch)
    rows = (await db_session.execute(select(DHCPPoolRange).where(
        DHCPPoolRange.source_id == w.id))).scalars().all()
    assert len(rows) == 8, "排除 7 個單一位址＝切成 8 段（讀「在不在範圍內」的地方要的是這個）"
    assert {(r.pool_start, r.pool_end) for r in rows} == {(f"{NET}.100", f"{NET}.230")}


async def test_one_scope_is_one_pool(db_session, monkeypatch) -> None:
    from app.services.dhcp_usage import pool_usage
    sub = await _subnet(db_session)
    await _windows(db_session, monkeypatch)
    await _leased_like_windows(db_session, sub, 74)
    (pool, used, size), = await pool_usage(db_session)
    assert size == 124, "131 個位址扣掉 7 個排除＝124，跟 Windows 的統計一樣"
    assert used == 74
    assert (pool.start_ip, pool.end_ip) == (f"{NET}.100", f"{NET}.230"), "顯示整個範圍，不是第一段"
    assert pool.excluded == 7


async def test_excluded_addresses_with_records_do_not_count_as_used(db_session, monkeypatch) -> None:
    """排除的位址多半就是設了固定 IP 的設備，本來就有 IP 記錄 —— 它們不在可發放的量裡。"""
    from app.services.dhcp_usage import pool_usage
    sub = await _subnet(db_session)
    await _windows(db_session, monkeypatch)
    for n in EXCLUDED:
        db_session.add(IPAddress(subnet_id=sub.id, ip=f"{NET}.{n}"))
    await db_session.flush()
    (_, used, size), = await pool_usage(db_session)
    assert (used, size) == (0, 124)


async def test_scope_at_half_use_does_not_alert(db_session, monkeypatch, admin_user) -> None:
    from app.services.capacity_alert import check_dhcp_pools
    from app.services.dhcp_usage import pool_usage
    sub = await _subnet(db_session)
    await _windows(db_session, monkeypatch)
    await _leased_like_windows(db_session, sub, 74)
    assert await check_dhcp_pools(db_session, await pool_usage(db_session), threshold_pct=90) == 0


async def test_rebuilt_ranges_are_still_the_same_pool(db_session, monkeypatch, admin_user) -> None:
    """每輪同步都刪掉重建範圍（id 會變）；滿著的集區只能通知一次。"""
    from app.models.notification import Notification
    from app.services.capacity_alert import check_dhcp_pools
    from app.services.dhcp_usage import pool_usage
    from app.services import windows_dhcp as wd
    sub = await _subnet(db_session)
    w = await _windows(db_session, monkeypatch)
    await _leased_like_windows(db_session, sub, 120)
    assert await check_dhcp_pools(db_session, await pool_usage(db_session), threshold_pct=90) == 1
    await wd.sync_scopes(db_session, w)          # 下一輪同步：同樣的範圍、新的 id
    await db_session.flush()
    assert await check_dhcp_pools(db_session, await pool_usage(db_session), threshold_pct=90) == 0
    notes = (await db_session.execute(select(Notification).where(
        Notification.user_id == admin_user.id))).scalars().all()
    full = [n for n in notes if (n.params or {}).get("size") == 124]
    assert len(full) == 1
    assert full[0].params["excluded"] == 7
    assert f"{NET}.100–{NET}.230" in full[0].params["where"]


def test_alert_key_ignores_the_row_id() -> None:
    from app.services.capacity_alert import pool_alert_key

    class _P:
        def __init__(self, rid):
            self.id = rid
            self.source_type, self.source_id = "windows_dhcp", "w1"
            self.subnet_cidr, self.start_ip, self.end_ip = f"{NET}.0/24", f"{NET}.100", f"{NET}.230"

    assert pool_alert_key(_P(uuid.uuid4())) == pool_alert_key(_P(uuid.uuid4()))


async def test_fortigate_range_minus_exclusion_is_one_pool(db_session, monkeypatch) -> None:
    from app.services import fortigate as fg
    from app.services.dhcp_usage import pool_usage
    from app.models.fortigate import FortiGateFirewall
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    db_session.add(Subnet(section_id=sec.id, cidr="10.7.0.0/24"))
    fw = FortiGateFirewall(name=f"fg-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.1",
                           api_token_enc=b"x", api_token_nonce=b"x", vdoms=["root"])
    db_session.add(fw)
    await db_session.flush()

    async def fake(_fw, path, *, vdom=None, timeout=15.0):  # type: ignore[no-untyped-def]
        return [{"interface": "internal", "status": "enable",
                 "ip-range": [{"id": 1, "start-ip": "10.7.0.100", "end-ip": "10.7.0.200"}],
                 "exclude-range": [{"id": 1, "start-ip": "10.7.0.150", "end-ip": "10.7.0.159",
                                    "vci-match": "disable", "uci-match": "disable"}]}] \
            if path == fg.EP_DHCP_SERVERS else []
    monkeypatch.setattr(fg, "_api_get", fake)
    await fg.sync_dhcp_ranges(db_session, fw, ["root"])
    await db_session.flush()
    rows = (await db_session.execute(select(DHCPPoolRange).where(
        DHCPPoolRange.source_id == fw.id))).scalars().all()
    assert {(r.pool_start, r.pool_end) for r in rows} == {("10.7.0.100", "10.7.0.200")}
    (pool, _used, size), = await pool_usage(db_session)
    assert size == 91
    assert pool.excluded == 10


async def test_write_pools_carries_the_configured_range(db_session) -> None:
    """Technitium、Check Point、Kea、ISC 共用的寫入層。沒給原始範圍的照舊（原始範圍＝自己）。"""
    from app.services.dhcp_standalone import write_pools
    sid = uuid.uuid4()
    await write_pools(db_session, source_type="technitium", source_id=sid, source_name="t", engine="technitium",
                      pools=[{"subnet": "10.8.0.0/24", "start": "10.8.0.10", "end": "10.8.0.19",
                              "pool_start": "10.8.0.10", "pool_end": "10.8.0.50"},
                             {"subnet": "10.8.0.0/24", "start": "10.8.0.21", "end": "10.8.0.50",
                              "pool_start": "10.8.0.10", "pool_end": "10.8.0.50"},
                             {"subnet": "10.9.0.0/24", "start": "10.9.0.10", "end": "10.9.0.20"}])
    await db_session.flush()
    rows = (await db_session.execute(select(DHCPPoolRange).where(
        DHCPPoolRange.source_id == sid))).scalars().all()
    assert sorted((r.start_ip, r.pool_start, r.pool_end) for r in rows) == [
        ("10.8.0.10", "10.8.0.10", "10.8.0.50"), ("10.8.0.21", "10.8.0.10", "10.8.0.50"),
        ("10.9.0.10", None, None)]


def test_technitium_and_checkpoint_say_which_range_each_part_came_from() -> None:
    from app.services.checkpoint_gaia import parse_dhcp_server
    from app.services.technitium_dhcp import pool_parts
    assert pool_parts("10.8.0.10", "10.8.0.50", [{"startingAddress": "10.8.0.20",
                                                  "endingAddress": "10.8.0.20"}]) == [
        ("10.8.0.10", "10.8.0.19", "10.8.0.10", "10.8.0.50"),
        ("10.8.0.21", "10.8.0.50", "10.8.0.10", "10.8.0.50")]
    (sn,) = parse_dhcp_server({"enabled": True, "subnets": [{
        "subnet": "10.6.0.0", "netmask": 24, "enabled": True,
        "ip-pools": [{"start": "10.6.0.10", "end": "10.6.0.50", "include": "include", "enabled": True},
                     {"start": "10.6.0.100", "end": "10.6.0.120", "include": "include", "enabled": True},
                     {"start": "10.6.0.30", "end": "10.6.0.30", "include": "exclude", "enabled": True}]}]})
    assert sn["pool_parts"] == [
        ("10.6.0.10", "10.6.0.29", "10.6.0.10", "10.6.0.50"),
        ("10.6.0.31", "10.6.0.50", "10.6.0.10", "10.6.0.50"),
        ("10.6.0.100", "10.6.0.120", "10.6.0.100", "10.6.0.120")]


async def test_subnet_address_ranges_show_one_row_per_configured_range(db_session, monkeypatch) -> None:
    """子網路「位址範圍」頁：自動建立的範圍以前一段一列（8 列、第一列「已滿」），客戶看不出其他段還有空位。

    資料照樣一段一筆（IP 地圖的底線、「在 DHCP 範圍內」要的是段），顯示合成一列：整個範圍、總數扣掉排除、
    下一個可用位址跳過排除的位址；各段起訖另外給，地圖照段畫。
    """
    from app.services.ip_ranges import ranges_with_usage, sync_auto_dhcp_ranges
    sub = await _subnet(db_session)
    await _windows(db_session, monkeypatch)
    await sync_auto_dhcp_ranges(db_session)
    await _leased_like_windows(db_session, sub, 61)        # .100–.160 全滿（第一段）
    for n in EXCLUDED:                                      # 排除的位址多半是固定 IP 的設備
        db_session.add(IPAddress(subnet_id=sub.id, ip=f"{NET}.{n}"))
    await db_session.flush()
    (row,) = await ranges_with_usage(db_session, sub)
    assert (row["start_ip"], row["end_ip"]) == (f"{NET}.100", f"{NET}.230")
    assert (row["used"], row["size"], row["excluded"]) == (61, 124, 7)
    assert row["first_free"] == f"{NET}.162", ".161 是排除的位址，不是可用的下一個"
    assert len(row["segments"]) == 8
    assert row["auto"] is True


async def test_manual_ranges_are_untouched(db_session) -> None:
    from app.models.ip_range import IPRange
    from app.services.ip_ranges import ranges_with_usage
    sub = await _subnet(db_session)
    db_session.add(IPRange(subnet_id=sub.id, start_ip=f"{NET}.10", end_ip=f"{NET}.19", purpose="reserved"))
    await db_session.flush()
    (row,) = await ranges_with_usage(db_session, sub)
    assert (row["size"], row["excluded"], row.get("segments")) == (10, 0, None)
