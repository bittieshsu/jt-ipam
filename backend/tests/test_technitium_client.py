"""Technitium DNS Server 的 HTTP API 用戶端（DNS 與 DHCP 兩個整合共用）。

實機（15.6，測試用容器）確認過的行為：
- 錯誤一律是 HTTP 200，看 `status`：`ok` / `error`（`errorMessage`）/ `invalid-token`
- token 可以放 Query、POST 表單或 Authorization 標頭 —— 我們放 POST 表單：舊版也吃，而且不會出現在網址
  （網址會進反向代理的存取記錄、也可能被帶進錯誤訊息）
- 權限不足是 `status: error`、`errorMessage: "Access was denied."`；zone 有逐個 zone 的權限
- HTTP 轉 HTTPS 的轉址不跟：POST 被轉成 GET 時 token 會掉，對方只會回 invalid-token，看不出原因
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest
from app.core.config import get_settings
from app.services import technitium as tc

from tests.isoinsight_mock import MockServer, json_resp

TOKEN = "tok-secret-0123456789abcdef"


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setenv("OUTBOUND_ALLOW_CIDRS", "127.0.0.1/32")
    get_settings.cache_clear()
    s = MockServer()
    yield s
    s.close()
    get_settings.cache_clear()


def _cli(srv: MockServer, **kw) -> tc.TechnitiumClient:
    return tc.TechnitiumClient(api_url=srv.url + "/", token=TOKEN, verify_tls=False, **kw)


async def test_token_goes_in_the_post_body_not_the_url(srv) -> None:
    srv.route("POST", "/api/zones/list", json_resp({"status": "ok", "response": {"zones": []}}))
    out = await _cli(srv).call("/api/zones/list")
    assert out == {"zones": []}
    req = srv.requests[-1]
    assert req["method"] == "POST" and TOKEN not in req["path"] + req["query"]
    assert req["headers"]["content-type"].startswith("application/x-www-form-urlencoded")
    assert parse_qs(req["body"].decode())["token"] == [TOKEN]


async def test_parameters_are_sent_with_the_token(srv) -> None:
    srv.route("POST", "/api/zones/records/get", json_resp({"status": "ok", "response": {"records": []}}))
    await _cli(srv).call("/api/zones/records/get", domain="example.test", zone="example.test", listZone="true")
    body = parse_qs(srv.requests[-1]["body"].decode())
    assert body["domain"] == ["example.test"] and body["listZone"] == ["true"]


@pytest.mark.parametrize(("reply", "code"), [
    ({"status": "invalid-token", "errorMessage": "Invalid token or session expired."}, "technitium_invalid_token"),
    ({"status": "error", "errorMessage": "Access was denied."}, "technitium_denied"),
    ({"status": "error", "errorMessage": "No such zone was found: x"}, "technitium_error"),
    ({"status": "2fa-required"}, "technitium_error"),
])
async def test_api_errors_are_classified(srv, reply, code) -> None:
    srv.route("POST", "/api/dhcp/leases/list", json_resp(reply))
    with pytest.raises(tc.TechnitiumError) as ei:
        await _cli(srv).call("/api/dhcp/leases/list")
    assert ei.value.code == code
    assert ei.value.params["endpoint"] == "/api/dhcp/leases/list"
    assert TOKEN not in str(ei.value) and TOKEN not in json.dumps(ei.value.params)


async def test_non_json_http_errors_and_redirects(srv) -> None:
    c = _cli(srv)
    srv.route("POST", "/api/zones/list", (200, [("Content-Type", "text/html")], b"<html>login</html>"))
    with pytest.raises(tc.TechnitiumError) as ei:
        await c.call("/api/zones/list")
    assert ei.value.code == "technitium_not_json"
    srv.route("POST", "/api/zones/list", (500, [], b"boom"))
    with pytest.raises(tc.TechnitiumError) as ei:
        await c.call("/api/zones/list")
    assert ei.value.code == "technitium_http" and ei.value.params["status"] == 500
    # HTTP → HTTPS：不跟（POST 轉 GET 會把 token 弄丟），講出要改用的網址（不帶 Query）
    srv.route("POST", "/api/zones/list", (301, [("Location", "https://dns.example.net:53443/api/zones/list?x=1")], b""))
    with pytest.raises(tc.TechnitiumError) as ei:
        await c.call("/api/zones/list")
    assert ei.value.code == "technitium_redirect"
    assert ei.value.params["location"] == "https://dns.example.net:53443/api/zones/list"


async def test_connection_failure_keeps_the_underlying_reason(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_ALLOW_CIDRS", "127.0.0.1/32")
    get_settings.cache_clear()
    try:
        c = tc.TechnitiumClient(api_url="http://127.0.0.1:9", token=TOKEN, verify_tls=False)
        with pytest.raises(tc.TechnitiumError) as ei:
            await c.call("/api/zones/list")
        assert ei.value.code == "technitium_transport" and ei.value.params["reason"]
        assert TOKEN not in str(ei.value)
    finally:
        get_settings.cache_clear()


async def test_session_info_reports_version_and_permissions(srv) -> None:
    srv.route("POST", "/api/user/session/get", json_resp({
        "status": "ok", "username": "jtipam-ro", "tokenName": "jt-ipam", "token": TOKEN,
        "info": {"version": "15.6", "dnsServerDomain": "dns1", "permissions": {
            "Zones": {"canView": True, "canModify": False, "canDelete": False},
            "DhcpServer": {"canView": True, "canModify": True, "canDelete": False},
            "Settings": {"canView": False, "canModify": False, "canDelete": False}}}}))
    info = await _cli(srv).session()
    assert info == {"version": "15.6", "server": "dns1", "user": "jtipam-ro",
                    "zones": {"view": True, "modify": False}, "dhcp": {"view": True, "modify": True}}
