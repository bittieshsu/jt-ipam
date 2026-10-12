"""DHCP 排除區間（0207，客戶 2026-10-12）。

客戶的 Windows DHCP 把設了固定 IP 的設備一個一個排除在發放範圍外（Address Pool 裡紅色 X 的那幾筆），
jt-ipam 以前不讀排除區間：那些位址被標成「在 DHCP 範圍內」，詳細資料還提示「建議把它排除在發放範圍外」。

- 發放範圍寫入前扣掉排除區間（讀範圍的地方因此自動正確）
- 排除區間另外記下來，IP 上正面標示「DHCP 排除」
- 讀取失敗保留既有資料（不可以讀一半就照寫）
"""
from __future__ import annotations

import uuid

import pytest
from app.models.address import IPAddress
from app.models.dhcp import DHCPExclusion, DHCPPoolRange, DHCPReservation
from app.models.section import Section
from app.models.subnet import Subnet
from app.services.dhcp_exclusions import Exclusion, clip, replace_exclusions, subtract
from sqlalchemy import select

pytestmark = pytest.mark.anyio


# ─────────────────── 區間相減 ───────────────────
def test_single_addresses_are_cut_out_of_the_range() -> None:
    """客戶的樣子：100–230 裡挖掉幾個單一位址。"""
    got = subtract("198.51.100.100", "198.51.100.230",
                   [("198.51.100.210", "198.51.100.210"), ("198.51.100.180", "198.51.100.180"),
                    ("198.51.100.195", "198.51.100.195")])
    assert got == [("198.51.100.100", "198.51.100.179"), ("198.51.100.181", "198.51.100.194"),
                   ("198.51.100.196", "198.51.100.209"), ("198.51.100.211", "198.51.100.230")]


def test_edges_overlaps_and_garbage() -> None:
    r = ("10.0.0.10", "10.0.0.50")
    assert subtract(*r, [("10.0.0.10", "10.0.0.12")]) == [("10.0.0.13", "10.0.0.50")]       # 開頭
    assert subtract(*r, [("10.0.0.48", "10.0.0.60")]) == [("10.0.0.10", "10.0.0.47")]       # 結尾、超出
    assert subtract(*r, [("10.0.0.20", "10.0.0.30"), ("10.0.0.25", "10.0.0.35")]) == [
        ("10.0.0.10", "10.0.0.19"), ("10.0.0.36", "10.0.0.50")]                            # 互相重疊
    assert subtract(*r, [("10.0.0.1", "10.0.0.99")]) == []                                   # 整段排除
    assert subtract(*r, [("10.0.1.1", "10.0.1.9")]) == [r]                                   # 範圍外
    assert subtract(*r, [("not-an-ip", "10.0.0.20"), ("2001:db8::1", "2001:db8::9")]) == [r]  # 壞資料略過
    assert subtract("garbage", "10.0.0.5", []) == []
    assert subtract("2001:db8::10", "2001:db8::20", [("2001:db8::15", "2001:db8::15")]) == [
        ("2001:db8::10", "2001:db8::14"), ("2001:db8::16", "2001:db8::20")]


def test_clip_keeps_only_the_part_inside_the_range() -> None:
    assert clip("10.0.0.10", "10.0.0.50", [("10.0.0.5", "10.0.0.12"), ("10.0.1.1", "10.0.1.2")]) == [
        ("10.0.0.10", "10.0.0.12")]


async def test_replace_only_touches_its_own_rows(db_session) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    await replace_exclusions(db_session, source_type="windows_dhcp", source_id=a, source_name="a",
                             engine="windows", rows=[Exclusion("10.0.0.5", "10.0.0.5")])
    await replace_exclusions(db_session, source_type="fortigate", source_id=b, source_name="b",
                             engine="fortigate", rows=[Exclusion("10.1.0.5", "10.1.0.6")])
    await replace_exclusions(db_session, source_type="windows_dhcp", source_id=a, source_name="a",
                             engine="windows", rows=[Exclusion("10.0.0.7", "10.0.0.7"),
                                                     Exclusion("10.0.0.7", "10.0.0.7")])
    rows = (await db_session.execute(select(DHCPExclusion))).scalars().all()
    assert sorted((r.source_type, r.start_ip, r.end_ip) for r in rows) == [
        ("fortigate", "10.1.0.5", "10.1.0.6"), ("windows_dhcp", "10.0.0.7", "10.0.0.7")]


# ─────────────────── Windows DHCP ───────────────────
class _Win:
    def __init__(self, scopes, exclusions=None, reservations=None, fail=()):
        self.scopes, self.ex, self.res, self.fail = scopes, exclusions or {}, reservations or {}, set(fail)

    def get_scopes(self):
        return self.scopes

    def get_exclusions(self, sid):
        from app.services.windows_dhcp import WindowsDhcpError
        if ("ex", sid) in self.fail:
            raise WindowsDhcpError("PowerShell error (rc=1): access denied")
        return self.ex.get(sid, [])

    def get_reservations(self, sid):
        from app.services.windows_dhcp import WindowsDhcpError
        if ("res", sid) in self.fail:
            raise WindowsDhcpError("PowerShell error (rc=1): access denied")
        return self.res.get(sid, [])

    def get_leases(self, sid):
        return []


SCOPE = {"ScopeId": "198.51.100.0", "SubnetMask": "255.255.255.0", "StartRange": "198.51.100.100",
         "EndRange": "198.51.100.230", "Name": "LAN", "State": "Active"}
EXCL = {"198.51.100.0": [{"StartRange": "198.51.100.210", "EndRange": "198.51.100.210"},
                         {"StartRange": {"IPAddressToString": "198.51.100.180"},
                          "EndRange": {"IPAddressToString": "198.51.100.180"}}]}


async def _win(db_session):  # type: ignore[no-untyped-def]
    from app.models.windows_dhcp import WindowsDhcpServer
    w = WindowsDhcpServer(name=f"win-{uuid.uuid4().hex[:6]}", host="dhcp.corp.example.com",
                          username="CORP\\svc", password_enc=b"x", password_nonce=b"x")
    db_session.add(w)
    await db_session.flush()
    return w


async def _pools(db_session, st, sid):  # type: ignore[no-untyped-def]
    rows = (await db_session.execute(select(DHCPPoolRange).where(
        DHCPPoolRange.source_type == st, DHCPPoolRange.source_id == sid))).scalars().all()
    return sorted((r.start_ip, r.end_ip) for r in rows)


async def _excl(db_session, st, sid):  # type: ignore[no-untyped-def]
    rows = (await db_session.execute(select(DHCPExclusion).where(
        DHCPExclusion.source_type == st, DHCPExclusion.source_id == sid))).scalars().all()
    return sorted((r.start_ip, r.end_ip) for r in rows)


def _key(t):  # type: ignore[no-untyped-def]
    import ipaddress
    return int(ipaddress.ip_address(t[0]))


async def test_windows_exclusions_are_cut_out_and_recorded(db_session, monkeypatch) -> None:
    from app.services import windows_dhcp as wd
    w = await _win(db_session)
    monkeypatch.setattr(wd, "_client", lambda _i: _Win([SCOPE], EXCL))
    assert await wd.sync_scopes(db_session, w) == 3
    assert sorted(await _pools(db_session, "windows_dhcp", w.id), key=_key) == [
        ("198.51.100.100", "198.51.100.179"), ("198.51.100.181", "198.51.100.209"),
        ("198.51.100.211", "198.51.100.230")]
    assert await _excl(db_session, "windows_dhcp", w.id) == [
        ("198.51.100.180", "198.51.100.180"), ("198.51.100.210", "198.51.100.210")]


async def test_windows_exclusion_read_failure_keeps_previous_data(db_session, monkeypatch) -> None:
    """排除區間讀不到時不可以寫完整範圍（等於又把那些位址當成會發放）：整段失敗、保留上一輪。"""
    from app.services import windows_dhcp as wd
    w = await _win(db_session)
    monkeypatch.setattr(wd, "_client", lambda _i: _Win([SCOPE], EXCL))
    await wd.sync_scopes(db_session, w)
    before = (await _pools(db_session, "windows_dhcp", w.id), await _excl(db_session, "windows_dhcp", w.id))
    monkeypatch.setattr(wd, "_client", lambda _i: _Win([SCOPE], EXCL, fail={("ex", "198.51.100.0")}))
    with pytest.raises(wd.WindowsDhcpError):
        await wd.sync_scopes(db_session, w)
    assert (await _pools(db_session, "windows_dhcp", w.id), await _excl(db_session, "windows_dhcp", w.id)) == before


async def test_windows_reservation_scope_failure_keeps_previous(db_session, monkeypatch) -> None:
    """單一 scope 的保留讀不到：以前略過那個 scope 照樣「取代」→ 那個 scope 的固定分配全被清掉。"""
    from app.services import windows_dhcp as wd
    w = await _win(db_session)
    res = {"198.51.100.0": [{"IPAddress": "198.51.100.150", "ClientId": "aa-bb-cc-dd-ee-01", "Name": "printer"}]}
    monkeypatch.setattr(wd, "_client", lambda _i: _Win([SCOPE], reservations=res))
    assert await wd.sync_reservations(db_session, w) == 1
    monkeypatch.setattr(wd, "_client", lambda _i: _Win([SCOPE], reservations=res, fail={("res", "198.51.100.0")}))
    with pytest.raises(wd.WindowsDhcpError):
        await wd.sync_reservations(db_session, w)
    rows = (await db_session.execute(select(DHCPReservation).where(
        DHCPReservation.source_id == w.id))).scalars().all()
    assert [r.ip for r in rows] == ["198.51.100.150"]


async def test_ip_list_and_detail_show_the_exclusion(client, auth_headers, db_session, monkeypatch) -> None:
    from app.services import windows_dhcp as wd
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    db_session.add_all([IPAddress(subnet_id=sub.id, ip=f"198.51.100.{n}") for n in (150, 210)])
    w = await _win(db_session)
    monkeypatch.setattr(wd, "_client", lambda _i: _Win([SCOPE], EXCL))
    await wd.sync_scopes(db_session, w)
    await db_session.commit()

    r = await client.get(f"/api/v1/addresses?subnet_id={sub.id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    by = {x["ip"].split("/")[0]: x for x in r.json()["items"]}
    assert by["198.51.100.210"]["in_dhcp_range"] is False, "排除的位址不會發放，不是「在 DHCP 範圍內」"
    ex = by["198.51.100.210"]["dhcp_exclusion"]
    assert ex["source_name"] == w.name
    assert ex["engine"] == "windows"
    assert (ex["start"], ex["end"]) == ("198.51.100.210", "198.51.100.210")
    assert by["198.51.100.150"]["in_dhcp_range"] is True
    assert by["198.51.100.150"]["dhcp_exclusion"] is None

    d = await client.get(f"/api/v1/addresses/{by['198.51.100.210']['id']}", headers=auth_headers)
    assert d.status_code == 200, d.text
    assert d.json()["dhcp_exclusion"]["source_name"] == w.name

    # 前端「DHCP 範圍」標籤是拿整份範圍清單比對的：清單裡也不可以再包含排除的位址
    rr = await client.get("/api/v1/dhcp-ranges", headers=auth_headers)
    assert rr.status_code == 200, rr.text
    import ipaddress
    n = int(ipaddress.ip_address("198.51.100.210"))
    assert not any(int(ipaddress.ip_address(x["start_ip"])) <= n <= int(ipaddress.ip_address(x["end_ip"]))
                   for x in rr.json() if x["source_type"] == "windows_dhcp")


async def test_forgetting_the_integration_removes_its_exclusions(db_session) -> None:
    from app.services.integration_cleanup import forget_instance
    sid = uuid.uuid4()
    await replace_exclusions(db_session, source_type="windows_dhcp", source_id=sid, source_name="w",
                             engine="windows", rows=[Exclusion("10.0.0.5", "10.0.0.5")])
    await forget_instance(db_session, source="windows_dhcp", source_id=sid)
    assert (await db_session.execute(select(DHCPExclusion))).scalars().all() == []


# ─────────────────── FortiGate ───────────────────
async def _fg(db_session):  # type: ignore[no-untyped-def]
    from app.models.fortigate import FortiGateFirewall
    fw = FortiGateFirewall(name=f"fgt-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.1",
                           api_token_enc=b"x", api_token_nonce=b"x", vdoms=["root"])
    db_session.add(fw)
    await db_session.flush()
    return fw


def _fg_api(monkeypatch, servers):  # type: ignore[no-untyped-def]
    from app.services import fortigate as fg

    async def fake(_fw, path, *, vdom=None, timeout=15.0):  # type: ignore[no-untyped-def]
        return servers if path == fg.EP_DHCP_SERVERS else []
    monkeypatch.setattr(fg, "_api_get", fake)


async def test_fortigate_exclude_range_is_cut_out(db_session, monkeypatch) -> None:
    """exclude-range 以前沒讀；只對特定用戶端（vci／uci 比對）的排除不是整段排除，不扣。"""
    from app.services import fortigate as fg
    fw = await _fg(db_session)
    _fg_api(monkeypatch, [{
        "interface": "internal", "status": "enable",
        "ip-range": [{"id": 1, "start-ip": "10.7.0.100", "end-ip": "10.7.0.200"}],
        "exclude-range": [{"id": 1, "start-ip": "10.7.0.150", "end-ip": "10.7.0.159",
                           "vci-match": "disable", "uci-match": "disable"},
                          {"id": 2, "start-ip": "10.7.0.170", "end-ip": "10.7.0.170",
                           "vci-match": "enable", "uci-match": "disable"}],
    }])
    assert await fg.sync_dhcp_ranges(db_session, fw, ["root"]) == 2
    assert sorted(await _pools(db_session, "fortigate", fw.id), key=_key) == [
        ("10.7.0.100", "10.7.0.149"), ("10.7.0.160", "10.7.0.200")]
    assert await _excl(db_session, "fortigate", fw.id) == [("10.7.0.150", "10.7.0.159")]


async def test_fortigate_assign_and_block_rules_are_not_reservations(db_session, monkeypatch) -> None:
    """reserved-address 的 action：reserved＝固定分配；assign＝跟一般用戶端一樣動態發放、block＝不發。"""
    from app.services import fortigate as fg
    fw = await _fg(db_session)
    _fg_api(monkeypatch, [{"interface": "internal", "status": "enable", "reserved-address": [
        {"id": 1, "type": "mac", "ip": "10.7.0.20", "mac": "aa:bb:cc:00:00:01", "action": "reserved"},
        {"id": 2, "type": "mac", "ip": "10.7.0.21", "mac": "aa:bb:cc:00:00:02"},              # 沒寫＝舊行為
        {"id": 3, "type": "mac", "ip": "10.7.0.22", "mac": "aa:bb:cc:00:00:03", "action": "assign"},
        {"id": 4, "type": "mac", "ip": "10.7.0.23", "mac": "aa:bb:cc:00:00:04", "action": "block"},
    ]}])
    assert await fg.sync_dhcp_reservations(db_session, fw, ["root"]) == 2
    rows = (await db_session.execute(select(DHCPReservation).where(
        DHCPReservation.source_id == fw.id))).scalars().all()
    assert sorted(r.ip for r in rows) == ["10.7.0.20", "10.7.0.21"]
