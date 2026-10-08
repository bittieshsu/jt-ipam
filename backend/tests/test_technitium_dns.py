"""Technitium 當 DNS 伺服器（DNSServer.type = technitium）：zone 清單與 A／AAAA／PTR／CNAME 紀錄。

回應形狀照實機 15.6 錄下來的（測試用容器）。重點：
- 停用的 zone、停用的紀錄不算（伺服器不會回答它們）；Stub／Catalog 這類 zone 沒有自己的位址紀錄，不讀
- 讀不到某個 zone（權限是逐個 zone 給的）→ 那個 zone 丟 DNSAdapterError，pull_server 記成問題、不清它的舊資料
"""
from __future__ import annotations

import pytest
from app.core.config import get_settings
from app.services.dns.base import DNSAdapterError
from app.services.dns.technitium import TechnitiumDNSAdapter

from tests.isoinsight_mock import MockServer, json_resp

ZONES = {"status": "ok", "response": {"zones": [
    {"name": "example.test", "type": "Primary", "disabled": False},
    {"name": "250.31.172.in-addr.arpa", "type": "Primary", "disabled": False},
    {"name": "0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa", "type": "Secondary", "disabled": False},
    {"name": "corp.test", "type": "Forwarder", "disabled": False},
    {"name": "old.test", "type": "Primary", "disabled": True},
    {"name": "stub.test", "type": "Stub", "disabled": False},
    {"name": "catalog.test", "type": "Catalog", "disabled": False},
]}}


def _rec(name, typ, rdata, ttl=300, disabled=False):
    return {"name": name, "type": typ, "ttl": ttl, "disabled": disabled, "rData": rdata}


RECORDS = {"status": "ok", "response": {"zone": {"name": "example.test"}, "records": [
    _rec("example.test", "SOA", {"primaryNameServer": "ns1"}, 900),
    _rec("example.test", "NS", {"nameServer": "ns1"}, 14400),
    _rec("web.example.test", "A", {"ipAddress": "172.31.250.10"}),
    _rec("v6.example.test", "AAAA", {"ipAddress": "2001:db8::10"}, 600),
    _rec("www.example.test", "CNAME", {"cname": "web.example.test"}),
    _rec("gone.example.test", "A", {"ipAddress": "172.31.250.99"}, disabled=True),
    _rec("Laptop-01.Example.Test", "A", {"ipAddress": "172.31.250.100"}),
]}}
PTRS = {"status": "ok", "response": {"records": [
    _rec("10.250.31.172.in-addr.arpa", "PTR", {"ptrName": "web.example.test"}),
]}}


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setenv("OUTBOUND_ALLOW_CIDRS", "127.0.0.1/32")
    get_settings.cache_clear()
    s = MockServer()

    def records(req):  # type: ignore[no-untyped-def]
        from urllib.parse import parse_qs
        zone = parse_qs(req["body"].decode())["zone"][0]
        if zone == "example.test":
            return json_resp(RECORDS)
        if zone == "250.31.172.in-addr.arpa":
            return json_resp(PTRS)
        return json_resp({"status": "error", "errorMessage": "Access was denied."})
    s.route("POST", "/api/zones/list", json_resp(ZONES))
    s.route("POST", "/api/zones/records/get", records)
    yield s
    s.close()
    get_settings.cache_clear()


def _adapter(srv) -> TechnitiumDNSAdapter:  # type: ignore[no-untyped-def]
    return TechnitiumDNSAdapter(api_url=srv.url, token="t0ken-xyz", verify_tls=False)


async def test_zones_skip_disabled_and_zones_without_address_records(srv) -> None:
    zones = await _adapter(srv).list_zones()
    assert [(z.name, z.kind) for z in zones] == [
        ("example.test", "forward"), ("250.31.172.in-addr.arpa", "reverse"),
        ("0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa", "reverse"), ("corp.test", "forward")]


async def test_records_map_values_and_skip_disabled_ones(srv) -> None:
    ad = _adapter(srv)
    recs = {(r.name, r.type, r.value, r.ttl) for r in await ad.list_records("example.test")}
    assert recs == {
        ("web.example.test", "A", "172.31.250.10", 300), ("v6.example.test", "AAAA", "2001:db8::10", 600),
        ("www.example.test", "CNAME", "web.example.test", 300), ("laptop-01.example.test", "A", "172.31.250.100", 300)}
    ptr = await ad.list_records("250.31.172.in-addr.arpa")
    assert [(r.name, r.type, r.value) for r in ptr] == [("10.250.31.172.in-addr.arpa", "PTR", "web.example.test")]
    # 整個 zone 一次讀（listZone），不是逐筆名稱
    from urllib.parse import parse_qs
    body = parse_qs(srv.hits("POST", "/api/zones/records/get")[0]["body"].decode())
    assert body["listZone"] == ["true"] and body["domain"] == ["example.test"]


async def test_a_zone_without_permission_is_reported_not_emptied(srv) -> None:
    with pytest.raises(DNSAdapterError) as ei:
        await _adapter(srv).list_records("corp.test")
    assert ei.value.code == "technitium_denied" and ei.value.params["zone"] == "corp.test"


async def test_healthcheck_lists_unreadable_zones(srv) -> None:
    srv.route("POST", "/api/user/session/get", json_resp({"status": "ok", "username": "jtipam-ro", "info": {
        "version": "15.6", "dnsServerDomain": "dns1",
        "permissions": {"Zones": {"canView": True, "canModify": False, "canDelete": False}}}}))
    out = await _adapter(srv).healthcheck()
    assert out["ok"] is True and out["version"] == "15.6" and out["user"] == "jtipam-ro"
    assert out["zones"] == 4 and out["unreadable_zones"] == ["0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa", "corp.test"]
    assert out["can_modify"] is False


async def test_write_back_is_not_offered(srv) -> None:
    from app.services.dns.base import DNSRecordOp
    with pytest.raises(DNSAdapterError) as ei:
        await _adapter(srv).upsert_record("example.test", DNSRecordOp("x.example.test", "A", "172.31.250.5"))
    assert ei.value.code == "dns_technitium_read_only"
