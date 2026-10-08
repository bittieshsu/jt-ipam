"""回歸測試：UCS 以外的 DNS adapter 連線失敗時，必須拋 DNSAdapterError
（而非漏出 winrm/dnspython/json 的原始例外讓 /dns/servers/{id}/test 變成無訊息 500）。
"""

from __future__ import annotations

import pytest
from app.services.dns.base import DNSAdapterError

HOST = "192.0.2.10"  # TEST-NET-1，非 loopback/private，過 SSRF 檢查


# ───────────────────────── Windows DNS (WinRM) ─────────────────────────

def test_windows_run_ps_wraps_connection_error():
    from app.services.dns.windows_dns import WindowsDNSAdapter
    a = WindowsDNSAdapter(host=HOST, username="u", password="p")

    def boom():
        raise ConnectionError("connection timed out")

    a._session = boom  # 模擬 winrm 連不上
    with pytest.raises(DNSAdapterError):
        a._run_ps("Get-DnsServer")


def _captured_session(monkeypatch):  # type: ignore[no-untyped-def]
    import winrm
    seen: dict = {}

    class FakeSession:
        def __init__(self, **kw):  # type: ignore[no-untyped-def]
            seen.update(kw)

    monkeypatch.setattr(winrm, "Session", FakeSession)
    return seen


def test_windows_validates_the_certificate_by_default(monkeypatch):
    from app.services.dns.windows_dns import WindowsDNSAdapter
    seen = _captured_session(monkeypatch)
    WindowsDNSAdapter(host=HOST, username="u", password="p")._session()
    assert seen["server_cert_validation"] == "validate"
    assert seen["target"].startswith("https://")


def test_windows_can_skip_certificate_validation_for_self_signed_winrm(monkeypatch):
    """客戶 2026-10-08：WinRM HTTPS 用自簽憑證 → CERTIFICATE_VERIFY_FAILED；比照其他整合可關閉驗證。"""
    from app.services.dns.windows_dns import WindowsDNSAdapter
    seen = _captured_session(monkeypatch)
    WindowsDNSAdapter(host=HOST, username="u", password="p", verify_tls=False)._session()
    assert seen["server_cert_validation"] == "ignore"
    assert seen["target"].startswith("https://"), "關閉驗證仍然走 HTTPS（加密照舊，只是不驗憑證）"


async def test_factory_passes_verify_tls_from_extra_config(monkeypatch):
    import json as _json
    from types import SimpleNamespace

    from app.services.dns import factory
    from app.services.dns.windows_dns import WindowsDNSAdapter

    async def fake_secret(*a, **k):  # type: ignore[no-untyped-def]
        return "p"

    monkeypatch.setattr(factory, "_load_secret", fake_secret)
    srv = SimpleNamespace(type="windows_dns", server_address=HOST, api_url=None,
                          extra_config=_json.dumps({"username": "u", "verify_tls": False}))
    a = await factory.get_adapter(None, srv)
    assert isinstance(a, WindowsDNSAdapter)
    assert a.verify_tls is False
    srv.extra_config = _json.dumps({"username": "u"})
    assert (await factory.get_adapter(None, srv)).verify_tls is True


def test_windows_http_5985_forces_ntlm_message_encryption(monkeypatch):
    """客戶 2026-10-08：Windows Server 2022 防火牆預設只開 WinRM HTTP 5985。
    走 HTTP 時一定要 NTLM 加密（message_encryption=always）：不支援就失敗，不可退回明文。"""
    from app.services.dns.windows_dns import WindowsDNSAdapter
    seen = _captured_session(monkeypatch)
    WindowsDNSAdapter(host=HOST, username="u", password="p", use_ssl=False, port=5985)._session()
    assert seen["target"] == f"http://{HOST}:5985/wsman"
    assert seen["message_encryption"] == "always"
    assert seen["transport"] == "ntlm"


async def test_factory_default_port_follows_the_scheme(monkeypatch):
    import json as _json
    from types import SimpleNamespace

    from app.services.dns import factory

    async def fake_secret(*a, **k):  # type: ignore[no-untyped-def]
        return "p"

    monkeypatch.setattr(factory, "_load_secret", fake_secret)
    srv = SimpleNamespace(type="windows_dns", server_address=HOST, api_url=None,
                          extra_config=_json.dumps({"username": "u", "use_ssl": False}))
    a = await factory.get_adapter(None, srv)
    assert (a.use_ssl, a.port) == (False, 5985)
    srv.extra_config = _json.dumps({"username": "u"})
    a = await factory.get_adapter(None, srv)
    assert (a.use_ssl, a.port) == (True, 5986)
    srv.extra_config = _json.dumps({"username": "u", "use_ssl": True, "winrm_port": 15986})
    assert (await factory.get_adapter(None, srv)).port == 15986


def test_windows_self_signed_error_says_how_to_fix():
    import ssl

    from app.services.dns.windows_dns import WindowsDNSAdapter
    a = WindowsDNSAdapter(host=HOST, username="u", password="p")

    def boom():
        raise ConnectionError(ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"))

    a._session = boom
    with pytest.raises(DNSAdapterError) as ei:
        a._run_ps("Get-DnsServer")
    assert ei.value.code == "dns_winrm_cert"
    assert "self-signed" in ei.value.params["reason"]


# ───────────────────────── BIND 9 (dnspython) ─────────────────────────

async def test_bind9_healthcheck_wraps_oserror(monkeypatch):
    from app.services.dns import bind9
    a = bind9.Bind9Adapter(
        server_address=HOST, tsig_keyname="", tsig_secret=None, zones=["example.com"],
    )

    def refused(*args, **kwargs):
        raise ConnectionRefusedError("connection refused")  # OSError，非 DNSException

    monkeypatch.setattr(bind9.dns.query, "udp", refused)
    with pytest.raises(DNSAdapterError):
        await a.healthcheck()


# ───────────────────────── PowerDNS (HTTP API) ─────────────────────────

async def test_powerdns_healthcheck_wraps_non_json(monkeypatch):
    from app.services.dns import powerdns

    class _Resp:
        status_code = 200

        def json(self):
            raise ValueError("not json")  # 認證失敗回 200 + HTML 登入頁

    async def fake_safe_request(method, url, **kw):
        return _Resp()

    monkeypatch.setattr(powerdns, "safe_request", fake_safe_request)
    a = powerdns.PowerDNSAdapter(api_url="https://pdns.example.com", api_key="k")
    with pytest.raises(DNSAdapterError):
        await a.healthcheck()


# ───────────────────────── OPNsense Unbound (HTTP API) ─────────────────────────

async def test_unbound_get_wraps_non_json(monkeypatch):
    from app.services.dns import unbound_opnsense

    class _Resp:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    async def fake_safe_request(method, url, **kw):
        return _Resp()

    monkeypatch.setattr(unbound_opnsense, "safe_request", fake_safe_request)
    a = unbound_opnsense.UnboundOPNsenseAdapter(
        api_url="https://opnsense.example.com", api_key="k", api_secret="s",
    )
    with pytest.raises(DNSAdapterError):
        await a.healthcheck()
