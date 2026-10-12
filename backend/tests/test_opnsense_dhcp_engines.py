"""OPNsense 三種 DHCP 引擎的發放範圍與固定分配（2026-10-12 全 DHCP 來源檢查）。

以前發放範圍只讀 Kea；ISC（config.xml）與 dnsmasq（25.1 起新安裝的預設 DHCP）都讀不到，
dnsmasq 的固定分配也沒讀。dnsmasq 的欄位照 OPNsense 原始碼的模型（Dnsmasq.xml）：
主機的 ip／hwaddr 都可以是多筆（逗號分隔），ignore＝不回應 DHCP；沒有 hwaddr／client_id 的主機
只是 DNS 主機覆寫，不是固定分配。IPv6 範圍可以用 constructor 以介面前綴組位址（::1000 這種），
那不是完整位址。
"""
from __future__ import annotations

import uuid

import pytest
from app.models.dhcp import DHCPPoolRange, DHCPReservation
from app.models.firewall import OPNsenseFirewall
from app.services import opnsense_firewall as fw
from sqlalchemy import select

pytestmark = pytest.mark.anyio

NOT_FOUND = "404 not found"


async def _fw(db_session):  # type: ignore[no-untyped-def]
    f = OPNsenseFirewall(name=f"opn-{uuid.uuid4().hex[:6]}", api_url="https://192.0.2.1",
                         api_key_enc=b"x", api_key_nonce=b"x", api_secret_enc=b"x", api_secret_nonce=b"x")
    db_session.add(f)
    await db_session.flush()
    return f


def _api(monkeypatch, routes, config_xml=None):  # type: ignore[no-untyped-def]
    calls: list[str] = []

    async def fake_get(_fw, path, timeout=8.0):  # type: ignore[no-untyped-def]
        calls.append(path)
        if path in routes:
            v = routes[path]
            if isinstance(v, Exception):
                raise v
            return v
        raise fw.OPNsenseError(f"OPNsense GET {path}: {NOT_FOUND}")

    async def fake_xml(_fw):  # type: ignore[no-untyped-def]
        calls.append("config.xml")
        if config_xml is None:
            raise fw.OPNsenseError("legacy config download: HTTP 403, len=0")
        return config_xml
    monkeypatch.setattr(fw, "_api_get", fake_get)
    monkeypatch.setattr(fw, "_download_config_xml", fake_xml)
    return calls


async def _pools(db_session, sid):  # type: ignore[no-untyped-def]
    rows = (await db_session.execute(select(DHCPPoolRange).where(DHCPPoolRange.source_id == sid))).scalars().all()
    return sorted(((r.subnet_cidr, r.start_ip, r.end_ip, r.family, r.source) for r in rows),
                  key=lambda t: (t[0] or "", t[1]))


async def _res(db_session, sid):  # type: ignore[no-untyped-def]
    rows = (await db_session.execute(select(DHCPReservation).where(DHCPReservation.source_id == sid))).scalars().all()
    return sorted((r.ip, r.mac, r.hostname, r.source) for r in rows)


DNSMASQ_ON = {"dnsmasq": {"enable": "1"}}
KEA_EMPTY = {"/api/kea/dhcpv4/searchSubnet": {"rows": []}, "/api/kea/dhcpv4/searchReservation": {"rows": []}}


# ─────────────────── dnsmasq ───────────────────
async def test_dnsmasq_ranges(db_session, monkeypatch) -> None:
    f = await _fw(db_session)
    calls = _api(monkeypatch, {**KEA_EMPTY, "/api/dnsmasq/settings/get": DNSMASQ_ON,
                               "/api/dnsmasq/settings/searchRange": {"rows": [
        {"interface": "LAN", "start_addr": "198.51.100.100", "end_addr": "198.51.100.199",
         "subnet_mask": "255.255.255.0", "constructor": ""},
        {"interface": "LAN", "start_addr": "::1000", "end_addr": "::2000", "constructor": "LAN"},  # 介面前綴組的
        {"interface": "OPT1", "start_addr": "203.0.113.10", "end_addr": "", "constructor": ""},   # 沒有結束位址
    ]}})
    res = await fw.sync_dhcp_ranges(db_session, f)
    assert res["ranges"] == 1
    assert await _pools(db_session, f.id) == [("198.51.100.0/24", "198.51.100.100", "198.51.100.199", 4, "dnsmasq")]
    assert "config.xml" not in calls, "有 dnsmasq 範圍就不必再下載整份設定檔"


async def test_dnsmasq_switched_off_is_not_a_dhcp_server(db_session, monkeypatch) -> None:
    f = await _fw(db_session)
    _api(monkeypatch, {**KEA_EMPTY, "/api/dnsmasq/settings/get": {"dnsmasq": {"enable": "0"}},
                       "/api/dnsmasq/settings/searchRange": {"rows": [
        {"interface": "LAN", "start_addr": "198.51.100.100", "end_addr": "198.51.100.199"}]}},
        config_xml="<opnsense><dhcpd/></opnsense>")
    assert (await fw.sync_dhcp_ranges(db_session, f))["ranges"] == 0


async def test_dnsmasq_hosts_with_a_mac_are_reservations(db_session, monkeypatch) -> None:
    f = await _fw(db_session)
    calls = _api(monkeypatch, {**KEA_EMPTY, "/api/dnsmasq/settings/get": DNSMASQ_ON,
                               "/api/dnsmasq/settings/searchHost": {"rows": [
        {"host": "nas", "ip": "198.51.100.5", "hwaddr": "aa:bb:cc:00:00:01", "ignore": "0", "descr": "rack"},
        {"host": "www", "ip": "198.51.100.6", "hwaddr": "", "client_id": "", "ignore": "0"},       # 只是 DNS 覆寫
        {"host": "blocked", "ip": "198.51.100.7", "hwaddr": "aa:bb:cc:00:00:07", "ignore": "1"},  # 不回應 DHCP
        {"host": "dual", "ip": "198.51.100.8,fd00::8", "hwaddr": "aa:bb:cc:00:00:08,aa:bb:cc:00:00:09"},
        {"host": "cid", "ip": "198.51.100.10", "hwaddr": "", "client_id": "01:aa:bb:cc:00:00:10"},
    ]}})
    assert await fw.sync_dhcp_reservations(db_session, f) == 6
    assert await _res(db_session, f.id) == [
        ("198.51.100.10", None, "cid", "dnsmasq"),
        ("198.51.100.5", "aa:bb:cc:00:00:01", "nas", "dnsmasq"),
        ("198.51.100.8", "aa:bb:cc:00:00:08", "dual", "dnsmasq"),
        ("198.51.100.8", "aa:bb:cc:00:00:09", "dual", "dnsmasq"),
        ("fd00::8", "aa:bb:cc:00:00:08", "dual", "dnsmasq"),
        ("fd00::8", "aa:bb:cc:00:00:09", "dual", "dnsmasq"),
    ]
    assert "config.xml" not in calls


async def test_dnsmasq_read_error_keeps_previous_reservations(db_session, monkeypatch) -> None:
    f = await _fw(db_session)
    _api(monkeypatch, {**KEA_EMPTY, "/api/dnsmasq/settings/get": DNSMASQ_ON,
                       "/api/dnsmasq/settings/searchHost": {"rows": [
        {"host": "nas", "ip": "198.51.100.5", "hwaddr": "aa:bb:cc:00:00:01"}]}})
    await fw.sync_dhcp_reservations(db_session, f)
    _api(monkeypatch, {**KEA_EMPTY, "/api/dnsmasq/settings/get": DNSMASQ_ON,
                       "/api/dnsmasq/settings/searchHost": fw.OPNsenseError("OPNsense GET x: 500 boom")})
    with pytest.raises(fw.OPNsenseError):
        await fw.sync_dhcp_reservations(db_session, f)
    assert [r[0] for r in await _res(db_session, f.id)] == ["198.51.100.5"]


# ─────────────────── ISC（config.xml）───────────────────
ISC_XML = """<?xml version="1.0"?>
<opnsense>
  <interfaces>
    <lan><ipaddr>192.0.2.1</ipaddr><subnet>24</subnet></lan>
    <opt1><ipaddr>dhcp</ipaddr></opt1>
    <opt2><ipaddr>10.9.0.1</ipaddr><subnet>24</subnet></opt2>
  </interfaces>
  <dhcpd>
    <lan>
      <enable>1</enable>
      <range><from>192.0.2.100</from><to>192.0.2.199</to></range>
      <staticmap><mac>aa:bb:cc:00:00:01</mac><ipaddr>192.0.2.10</ipaddr><hostname>nas</hostname></staticmap>
    </lan>
    <opt1>
      <enable>1</enable>
      <range><from>203.0.113.50</from><to>203.0.113.60</to></range>
    </opt1>
    <opt2>
      <range><from>10.9.0.100</from><to>10.9.0.150</to></range>
    </opt2>
  </dhcpd>
</opnsense>
"""


async def test_isc_ranges_come_from_config_xml(db_session, monkeypatch) -> None:
    """沒有 Kea、dnsmasq 範圍時讀 config.xml 的 ISC 設定；沒啟用的介面不算，CIDR 由介面位址推。"""
    f = await _fw(db_session)
    _api(monkeypatch, {**KEA_EMPTY}, config_xml=ISC_XML)
    res = await fw.sync_dhcp_ranges(db_session, f)
    assert res["ranges"] == 2
    assert await _pools(db_session, f.id) == [
        (None, "203.0.113.50", "203.0.113.60", 4, "isc"),
        ("192.0.2.0/24", "192.0.2.100", "192.0.2.199", 4, "isc")]


async def test_isc_config_download_failure_keeps_previous_ranges(db_session, monkeypatch) -> None:
    f = await _fw(db_session)
    _api(monkeypatch, {**KEA_EMPTY}, config_xml=ISC_XML)
    await fw.sync_dhcp_ranges(db_session, f)
    before = await _pools(db_session, f.id)
    _api(monkeypatch, {**KEA_EMPTY}, config_xml=None)
    with pytest.raises(fw.OPNsenseError):
        await fw.sync_dhcp_ranges(db_session, f)
    assert await _pools(db_session, f.id) == before


async def test_kea_ranges_still_win_without_downloading_config(db_session, monkeypatch) -> None:
    f = await _fw(db_session)
    calls = _api(monkeypatch, {"/api/kea/dhcpv4/searchSubnet": {"rows": [
        {"subnet": "10.0.0.0/24", "pools": "10.0.0.10-10.0.0.50"}]}}, config_xml=ISC_XML)
    assert (await fw.sync_dhcp_ranges(db_session, f))["ranges"] == 1
    assert await _pools(db_session, f.id) == [("10.0.0.0/24", "10.0.0.10", "10.0.0.50", 4, "kea")]
    assert "config.xml" not in calls


async def test_key_without_dnsmasq_privilege_keeps_working(db_session, monkeypatch) -> None:
    """OPNsense 的 API key 權限是逐項授予的：只給 Kea 的 key 讀 dnsmasq 會 403。
    以前不讀 dnsmasq 時這種站台是正常的，不可以因為多看一種引擎就整段失敗；結果裡註明讀不到。"""
    f = await _fw(db_session)
    _api(monkeypatch, {"/api/kea/dhcpv4/searchSubnet": {"rows": [
        {"subnet": "10.0.0.0/24", "pools": "10.0.0.10-10.0.0.50"}]},
        "/api/kea/dhcpv4/searchReservation": {"rows": [{"ip_address": "10.0.0.5", "hw_address": "aa:bb:cc:00:00:05"}]},
        "/api/dnsmasq/settings/get": fw.OPNsenseError("OPNsense GET /api/dnsmasq/settings/get: 403 Forbidden")})
    res = await fw.sync_dhcp_ranges(db_session, f)
    assert res["ranges"] == 1
    assert res.get("dnsmasq") == "forbidden"
    assert await fw.sync_dhcp_reservations(db_session, f) == 1
