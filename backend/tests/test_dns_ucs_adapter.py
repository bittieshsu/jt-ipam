"""UCS（UDM REST）adapter：反解 zone 的 PTR 以前從來沒讀進來（2026-10-10 比對群組實測發現）。

UDM 的物件形狀（正式站台 UCS 實測）：
- reverse_zone：dn 是 `zoneName=2.0.192.in-addr.arpa,cn=dns,...`，properties.subnet 是 `192.0.2`
- ptr_record：properties.address 是 zone 內的相對名稱（`145`），properties.ptr_record 是 FQDN 清單（帶結尾點）
"""
from __future__ import annotations

from typing import Any

from app.services.dns.ucs import UniventionUCSAdapter

FWD_DN = "zoneName=example.com,cn=dns,dc=example,dc=com"
REV_DN = "zoneName=2.0.192.in-addr.arpa,cn=dns,dc=example,dc=com"
REV6_DN = "zoneName=8.b.d.0.1.0.0.2.ip6.arpa,cn=dns,dc=example,dc=com"


def _obj(dn: str, **props: Any) -> dict[str, Any]:
    return {"dn": dn, "properties": props}


def _adapter(monkeypatch) -> UniventionUCSAdapter:
    ad = UniventionUCSAdapter(api_url="https://ucs.example.com", username="u", password="p")
    data: dict[tuple[str, str | None], list[dict[str, Any]]] = {
        ("/dns/forward_zone/", None): [_obj(FWD_DN, zone="example.com")],
        ("/dns/reverse_zone/", None): [_obj(REV_DN, subnet="192.0.2"), _obj(REV6_DN, subnet="2001:0db8")],
        ("/dns/host_record/", FWD_DN): [_obj("relativeDomainName=dc01," + FWD_DN, name="dc01", a=["192.0.2.5"])],
        ("/dns/alias/", FWD_DN): [],
        ("/dns/ptr_record/", REV_DN): [
            _obj("relativeDomainName=5," + REV_DN, address="5", ptr_record=["dc01.example.com."]),
            _obj("relativeDomainName=6," + REV_DN, address="6", ptr_record=["a.example.com.", "b.example.com."]),
        ],
        ("/dns/ptr_record/", REV6_DN): [],
    }

    async def fake_get(path: str, params: dict | None = None) -> dict:  # type: ignore[type-arg]
        key = (path, (params or {}).get("superordinate"))
        return {"_embedded": {"udm:object": data.get(key, [])}}
    monkeypatch.setattr(ad, "_get", fake_get)
    return ad


async def test_reverse_zone_names_come_from_the_dn(monkeypatch) -> None:
    zones = {(z.name, z.kind) for z in await _adapter(monkeypatch).list_zones()}
    assert ("2.0.192.in-addr.arpa", "reverse") in zones
    assert ("8.b.d.0.1.0.0.2.ip6.arpa", "reverse") in zones, "IPv6 反解 zone 不可以被當成 IPv4 倒過來"
    assert ("example.com", "forward") in zones


async def test_ptr_records_are_read_from_reverse_zones(monkeypatch) -> None:
    recs = await _adapter(monkeypatch).list_records("2.0.192.in-addr.arpa")
    assert sorted((r.name, r.type, r.value) for r in recs) == [
        ("5.2.0.192.in-addr.arpa", "PTR", "dc01.example.com"),
        ("6.2.0.192.in-addr.arpa", "PTR", "a.example.com"),
        ("6.2.0.192.in-addr.arpa", "PTR", "b.example.com"),
    ]


async def test_forward_zone_still_reads_hosts(monkeypatch) -> None:
    recs = await _adapter(monkeypatch).list_records("example.com")
    assert [(r.name, r.type, r.value) for r in recs] == [("dc01.example.com", "A", "192.0.2.5")]


APACHE_404 = ('<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\n<html><head>\n<title>404 Not Found</title>\n'
              '</head><body>\n<h1>Not Found</h1>\n<p>The requested URL was not found on this server.</p>\n</body></html>')


def _respond(monkeypatch, status: int, text: str, ctype: str) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from app.services.dns import ucs as ucs_mod

    async def fake(method, url, **_kw):  # type: ignore[no-untyped-def]
        return httpx.Response(status, text=text, headers={"content-type": ctype},
                              request=httpx.Request(method, url))
    monkeypatch.setattr(ucs_mod, "safe_request", fake)


async def test_missing_udm_rest_api_says_how_to_enable_it(monkeypatch) -> None:
    """使用者 2026-10-14：加了一台 UCS（ucs-dc2），同步失敗只看到一整段 Apache 的 404 HTML。
    那台根本沒裝 UDM REST API（Primary／Backup 預設才有）：要講出原因與怎麼裝，不要吐 HTML。"""
    import pytest

    from app.services.dns.base import DNSAdapterError
    _respond(monkeypatch, 404, APACHE_404, "text/html; charset=iso-8859-1")
    ad = UniventionUCSAdapter(api_url="https://ucs-b.example", username="u", password="p")
    with pytest.raises(DNSAdapterError) as ei:
        await ad.healthcheck()
    err = ei.value
    assert err.code == "dns_ucs_no_rest"
    assert "univention-directory-manager-rest" in str(err)
    assert "<html" not in str(err).lower() and "DOCTYPE" not in str(err)
    assert err.params.get("url") == "https://ucs-b.example/univention/udm/"


async def test_other_http_errors_do_not_dump_html(monkeypatch) -> None:
    import pytest

    from app.services.dns.base import DNSAdapterError
    _respond(monkeypatch, 503, "<html><head><title>503 Service Unavailable</title></head><body>x</body></html>",
             "text/html")
    ad = UniventionUCSAdapter(api_url="https://ucs-b.example", username="u", password="p")
    with pytest.raises(DNSAdapterError) as ei:
        await ad.healthcheck()
    assert ei.value.code == "dns_ucs_http"
    assert "<html" not in str(ei.value).lower()
    assert "503 Service Unavailable" in str(ei.value)


async def test_not_in_allowed_groups_says_which_setting(monkeypatch) -> None:
    """使用者 2026-10-14：dc2（Replica Directory Node）手動裝了 UDM REST API 後，同一個帳號 dc1 可以、dc2 回
    403 Not in allowed groups —— dc2 少了 directory/manager/rest/authorized-groups/* 的設定。要講出是哪個設定。"""
    import json

    import pytest

    from app.services.dns.base import DNSAdapterError
    body = json.dumps({"error": {"code": 403, "title": "", "message": "HTTP 403: Forbidden (Not in allowed groups.)",
                                 "error": {}}, "_links": {"self": [{"href": "https://ucs-b.example/univention/udm/"}]}})
    _respond(monkeypatch, 403, body, "application/json")
    ad = UniventionUCSAdapter(api_url="https://ucs-b.example", username="svc-dns", password="p")
    with pytest.raises(DNSAdapterError) as ei:
        await ad.healthcheck()
    err = ei.value
    assert err.code == "dns_ucs_not_allowed"
    assert err.params.get("user") == "svc-dns"
    assert "directory/manager/rest/authorized-groups" in str(err)
    assert "_links" not in str(err), "不把整包 JSON 塞進錯誤訊息"


async def test_json_errors_show_the_message_only(monkeypatch) -> None:
    import json

    import pytest

    from app.services.dns.base import DNSAdapterError
    body = json.dumps({"error": {"code": 500, "message": "HTTP 500: LDAP server unavailable"}, "_links": {}})
    _respond(monkeypatch, 500, body, "application/json")
    ad = UniventionUCSAdapter(api_url="https://ucs-b.example", username="u", password="p")
    with pytest.raises(DNSAdapterError) as ei:
        await ad.healthcheck()
    assert ei.value.code == "dns_ucs_http"
    assert "LDAP server unavailable" in str(ei.value)
    assert "_links" not in str(ei.value)


async def test_other_record_types_for_compare_groups(monkeypatch) -> None:
    """比對群組可以勾 CNAME／MX／TXT／NS／SRV（使用者 2026-10-14）：UCS 要拉得出這些類型。
    格式照 UCS 5.2 實機（dc1）：區域頂端的 a／nameserver／mx（{priority, mailserver}）／txt 在 forward_zone 上；
    SRV 的 name 是 {service, protocol, extension}、location 裡權重的欄位名是 UDM 拼錯的 weigtht；NS 委派在 ns_record。"""
    ad = UniventionUCSAdapter(api_url="https://ucs.example.com", username="u", password="p")
    data: dict[tuple[str, str | None], list[dict[str, Any]]] = {
        ("/dns/forward_zone/", None): [_obj(FWD_DN, zone="example.com", a=["192.0.2.5", "2001:db8::5"],
                                            nameserver=["dc01.example.com.", "dc02.example.com."],
                                            mx=[{"priority": "10", "mailserver": "mx1.example.com."}], txt=["v=spf1 -all"])],
        ("/dns/host_record/", FWD_DN): [_obj("relativeDomainName=dc01," + FWD_DN, name="dc01", a=["192.0.2.5"],
                                             mx=[], txt=["owner=it"])],
        ("/dns/alias/", FWD_DN): [_obj("relativeDomainName=book," + FWD_DN, name="book", cname="kb1.example.com.")],
        ("/dns/srv_record/", FWD_DN): [_obj("relativeDomainName=_ldap._tcp," + FWD_DN,
                                            name={"service": "ldap", "protocol": "tcp", "extension": None},
                                            location=[{"priority": "0", "weigtht": "100", "port": "389",
                                                       "server": "dc01.example.com."}])],
        ("/dns/txt_record/", FWD_DN): [_obj("relativeDomainName=_kerberos," + FWD_DN, name="_kerberos", txt=["EXAMPLE.COM"])],
        ("/dns/ns_record/", FWD_DN): [_obj("zoneName=sub," + FWD_DN, zone="sub", nameserver=["ns.sub.example.com."])],
    }

    async def fake_get(path: str, params: dict | None = None) -> dict:  # type: ignore[type-arg]
        return {"_embedded": {"udm:object": data.get((path, (params or {}).get("superordinate")), [])}}
    monkeypatch.setattr(ad, "_get", fake_get)
    got = {(r.name, r.type, r.value) for r in await ad.list_records("example.com")}
    assert ("example.com", "A", "192.0.2.5") in got, "區域頂端的 A 以前沒拉"
    assert ("example.com", "AAAA", "2001:db8::5") in got
    assert ("example.com", "NS", "dc01.example.com") in got
    assert ("example.com", "MX", "10 mx1.example.com") in got
    assert ("example.com", "TXT", "v=spf1 -all") in got
    assert ("dc01.example.com", "TXT", "owner=it") in got
    assert ("book.example.com", "CNAME", "kb1.example.com") in got
    assert ("_ldap._tcp.example.com", "SRV", "0 100 389 dc01.example.com") in got
    assert ("_kerberos.example.com", "TXT", "EXAMPLE.COM") in got
    assert ("sub.example.com", "NS", "ns.sub.example.com") in got
