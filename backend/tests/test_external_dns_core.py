"""外部 DNS 解析的核心（docs/SPEC_EXTERNAL_DNS_zh-TW.md；使用者 2026-10-19 定案）。"""
from __future__ import annotations

import socket
import threading
from typing import Any

import dns.message
import dns.rcode
import dns.rrset
import pytest

from app.services import external_dns as ed

pytestmark = pytest.mark.anyio


def test_settings_are_cleaned_and_bounded() -> None:
    s = ed.normalize_settings({
        "resolvers": [{"name": "Cloudflare", "address": "1.1.1.1"}, {"address": "not-an-ip"},
                      {"address": "9.9.9.9", "port": 99999, "enabled": False}],
        "public_domains": ["Example.COM.", "*.example.org", "bad domain", "example.com", "nodot"],
        "public_ranges": ["203.0.113.5/24", "nope"],
        "interval_hours": 0,
    })
    assert s["resolvers"] == [{"name": "Cloudflare", "address": "1.1.1.1", "port": 53, "enabled": True},
                              {"name": "9.9.9.9", "address": "9.9.9.9", "port": 53, "enabled": False}]
    assert s["public_domains"] == ["example.com", "example.org"]
    assert s["public_ranges"] == ["203.0.113.0/24"]
    assert s["interval_hours"] == 1
    assert s["query_authoritative"] is True


def test_only_names_under_public_domains_go_outside() -> None:
    doms = ["example.com"]
    assert ed.in_public_domains("example.com", doms)
    assert ed.in_public_domains("Chat.Example.com.", doms)
    assert not ed.in_public_domains("example.com.internal.lan", doms)
    assert not ed.in_public_domains("notexample.com", doms)


def _a(resolver: str, *ips: str, rcode: str = "NOERROR", auth: bool = False) -> dict[str, Any]:
    return {"resolver": resolver, "rcode": rcode, "a": list(ips), "aaaa": [], "cname": [], "authoritative": auth}


OWN = {"203.0.113.10": ed.OwnAddress("fw-a（opnsense）", firewall="fw-a")}


@pytest.mark.parametrize(("answers", "fws", "status"), [
    ([_a("cf", "203.0.113.10"), _a("g", "203.0.113.10")], {"fw-a"}, "consistent"),
    ([_a("cf", "203.0.113.10")], {"fw-b"}, "no_path"),
    ([_a("cf", "198.51.100.99")], {"fw-a"}, "foreign"),
    ([_a("cf", "203.0.113.10"), _a("g", "198.51.100.99")], set(), "mismatch"),
    ([_a("cf", "203.0.113.10"), _a("ns1", "198.51.100.99", auth=True)], set(), "mismatch"),
    ([_a("cf", rcode="NXDOMAIN"), _a("g", rcode="NODATA")], set(), "not_public"),
    ([_a("cf", rcode="NXDOMAIN"), _a("ns1", "203.0.113.10", auth=True)], set(), "mismatch"),
    ([_a("cf", rcode="TIMEOUT")], set(), "query_failed"),
    ([], set(), "not_checked"),
])
def test_classify(answers: list[dict[str, Any]], fws: set[str], status: str) -> None:
    assert ed.classify(answers, own=OWN, ranges=[], row_firewalls=fws)["status"] == status


def test_public_ranges_count_as_ours() -> None:
    r = ed.classify([_a("cf", "192.0.2.77")], own={}, ranges=["192.0.2.0/24"], row_firewalls={"fw-a"})
    assert r["status"] == "consistent"
    assert r["owners"] == {"192.0.2.77": "192.0.2.0/24"}


@pytest.fixture
def fake_dns(monkeypatch):
    """127.0.0.1 上的迷你 DNS：www.example.com → A 203.0.113.10（經 CNAME）；其他 NXDOMAIN。"""
    monkeypatch.setattr("app.core.net_guard.check_target", lambda *a, **k: [])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                continue
            q = dns.message.from_wire(data)
            r = dns.message.make_response(q)
            name = q.question[0].name.to_text()
            rdtype = q.question[0].rdtype
            if name == "www.example.com.":
                r.answer.append(dns.rrset.from_text("www.example.com.", 300, "IN", "CNAME", "edge.example.com."))
                if rdtype == dns.rdatatype.A:
                    r.answer.append(dns.rrset.from_text("edge.example.com.", 120, "IN", "A", "203.0.113.10"))
            else:
                r.set_rcode(dns.rcode.NXDOMAIN)
            sock.sendto(r.to_wire(), addr)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    yield sock.getsockname()[1]
    stop.set()
    th.join(1)
    sock.close()


import dns.rdatatype  # noqa: E402


async def test_query_resolver_reads_addresses_and_cname(fake_dns) -> None:
    a = await ed.query_resolver("www.example.com", "127.0.0.1", fake_dns, "local")
    assert (a.rcode, a.a, a.aaaa) == ("NOERROR", ["203.0.113.10"], [])
    assert a.cname == ["edge.example.com"]
    assert a.ttl == 120
    nx = await ed.query_resolver("gone.example.com", "127.0.0.1", fake_dns, "local")
    assert nx.rcode == "NXDOMAIN"


async def test_unreachable_resolver_is_a_timeout_not_an_exception(monkeypatch) -> None:
    monkeypatch.setattr("app.core.net_guard.check_target", lambda *a, **k: [])
    monkeypatch.setattr(ed, "QUERY_TIMEOUT", 0.3)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))          # 綁著但不回應
    try:
        a = await ed.query_resolver("www.example.com", "127.0.0.1", s.getsockname()[1], "dead")
    finally:
        s.close()
    assert a.rcode == "TIMEOUT"


async def test_blocked_resolver_address(monkeypatch) -> None:
    a = await ed.query_resolver("www.example.com", "169.254.169.254", 53, "meta")
    assert a.rcode == "BLOCKED"
