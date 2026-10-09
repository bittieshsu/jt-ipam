"""ISOinsight 整合的 HTTP client：用本機的 mock HTTP 伺服器驗證**真的送出去的東西**。

驗證項目（規格 §14）：真實的 HTTP 方法、Query／form／JSON 編碼（含特殊字元的帳密）、Cookie Jar
（多個 Cookie、Path、Secure、不跨來源共用）、Token 模式、錯誤分類（401／403／405／415／200 失敗／
HTML／3xx 不跟隨）、到期後只重新登入一次、重試上限、大小上限，以及帳密、Cookie、Token 不出現在
日誌、錯誤訊息與診斷記錄裡。

⚠️ 這些測試通過只代表 jt-ipam 的能力；GET／POST 哪一種在 ISOinsight 真機上可用仍待真機驗證。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

import pytest
from app.services.isoinsight import client as C
from app.services.isoinsight.errors import IsoError

from tests.isoinsight_mock import MockServer, json_resp, lease_body

USER = "op erator&1"
PW = "p a&s+s%w=rd?中=#"
ROWS = [{"ip": "192.0.2.20", "mac": "02:00:5e:00:53:20", "name": "laptop-07",
         "start_time": "2026/10/07 15:51:40", "end_time": "2026/10/07 19:51:40"}]


def _allow_loopback(_host, _addrs) -> None:
    """測試伺服器在 127.0.0.1：正式的出站政策一律擋迴路位址（另有一支測試確認這件事）。"""


@pytest.fixture
def srv():
    s = MockServer()
    yield s
    s.close()


def _settings(srv: MockServer, **kw) -> C.Settings:
    base = {"base_url": srv.url, "login_path": "/api/logon", "login_method": "POST", "post_format": "form",
            "username": USER, "password": PW, "auth_mode": "cookie", "lease_path": "/isosvc?act=DhcpLease",
            "connect_timeout": 2.0, "request_timeout": 2.0}
    base.update(kw)
    return C.Settings(**base)


def _login_ok(cookies=("SID=s1; Path=/",)):
    return 200, [("Content-Type", "application/json"), *[("Set-Cookie", c) for c in cookies]], b'{"ok":1}'


async def _run(s: C.Settings, **kw) -> C.Fetched:
    async with C.IsoClient(s, check=_allow_loopback, **kw) as cli:
        await cli.login()
        return await cli.fetch_leases()


# ── 登入請求的編碼 ──────────────────────────────────────────────────────────

async def test_get_login_encodes_credentials_as_query(srv) -> None:
    srv.route("GET", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", lease_body(ROWS))
    got = await _run(_settings(srv, login_method="GET"))
    assert got.status == 200
    (req,) = srv.hits("GET", "/api/logon")
    assert req["params"] == {"username": [USER], "password": [PW]}
    assert req["body"] == b""
    # 用 HTTP client 的編碼，不是字串拼接：原文的特殊字元不會直接出現在 Query
    assert "&1" not in req["query"].split("&password")[0].split("username=")[1]


async def test_post_form_login_is_form_encoded(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", lease_body(ROWS))
    await _run(_settings(srv))
    (req,) = srv.hits("POST", "/api/logon")
    assert req["headers"]["content-type"].startswith("application/x-www-form-urlencoded")
    assert parse_qs(req["body"].decode(), keep_blank_values=True) == {"username": [USER], "password": [PW]}
    assert req["query"] == ""


async def test_post_json_login_is_json(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", lease_body(ROWS))
    await _run(_settings(srv, post_format="json"))
    (req,) = srv.hits("POST", "/api/logon")
    assert req["headers"]["content-type"].startswith("application/json")
    assert json.loads(req["body"]) == {"username": USER, "password": PW}


async def test_custom_parameter_names(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", lease_body(ROWS))
    await _run(_settings(srv, username_param="acct", password_param="pass_word"))
    (req,) = srv.hits("POST", "/api/logon")
    assert parse_qs(req["body"].decode()) == {"acct": [USER], "pass_word": [PW]}


async def test_lease_request_keeps_its_query_and_asks_for_json(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", lease_body(ROWS))
    await _run(_settings(srv))
    (req,) = srv.hits("GET", "/isosvc")
    assert req["params"] == {"act": ["DhcpLease"]}
    assert req["headers"]["accept"].startswith("application/json")


# ── 登入失敗的分類：不換方法、不重送 ─────────────────────────────────────────

@pytest.mark.parametrize(("status", "code"), [
    (401, "AUTH_FAILED"), (403, "AUTH_FAILED"), (405, "METHOD_UNSUPPORTED"), (415, "METHOD_UNSUPPORTED"),
    (500, "SERVER_ERROR"), (404, "HTTP_ERROR"),
])
async def test_login_errors_are_classified_and_never_fall_back(srv, status, code) -> None:
    srv.route("POST", "/api/logon", (status, [("Content-Type", "text/plain")], b"no"))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == code and ei.value.stage == "login"
    # 只送了一次，沒有自動改用 GET 或 JSON 再送一次帳密
    assert len(srv.requests) == 1


async def test_login_redirect_is_not_followed_and_location_is_redacted(srv) -> None:
    srv.route("POST", "/api/logon", (302, [("Location", "https://198.51.100.9/login?next=/x&token=zz")], b""))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "UNEXPECTED_REDIRECT"
    assert ei.value.params["location"] == "https://198.51.100.9/login?<redacted>"
    assert "zz" not in str(ei.value)
    assert len(srv.requests) == 1


async def test_cookie_mode_without_any_cookie_is_an_unsupported_login_response(srv) -> None:
    srv.route("POST", "/api/logon", json_resp({"result": "fail"}))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "AUTH_RESPONSE_UNSUPPORTED"
    assert not srv.hits("GET", "/isosvc")


async def test_html_login_page_is_an_unsupported_flow(srv) -> None:
    srv.route("POST", "/api/logon", (200, [("Content-Type", "text/html")], b"<html><form>csrf</form></html>"))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "AUTH_FLOW_UNSUPPORTED"


async def test_login_429_is_rate_limited_with_retry_after(srv) -> None:
    srv.route("POST", "/api/logon", (429, [("Retry-After", "120")], b""))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "RATE_LIMITED" and ei.value.retry_after == 120


# ── Cookie ──────────────────────────────────────────────────────────────────

async def test_cookie_jar_keeps_several_cookies_and_honours_path_and_secure(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok(cookies=(
        "SESSIONX=a1; Path=/", "AUX=b2; Path=/isosvc", "OTHER=c3; Path=/other", "SEC=d4; Path=/; Secure",
        "OLD=e5; Path=/; Max-Age=0")))
    srv.route("GET", "/isosvc", lease_body(ROWS))
    await _run(_settings(srv))
    (req,) = srv.hits("GET", "/isosvc")
    assert req["cookies"] == {"SESSIONX": "a1", "AUX": "b2"}     # 名稱沒有寫死；Path／Secure／到期照規則


async def test_cookies_are_not_shared_between_sources_on_the_same_host() -> None:
    a, b = MockServer(), MockServer()
    try:
        for s, val in ((a, "A"), (b, "B")):
            s.route("POST", "/api/logon", _login_ok(cookies=(f"SID={val}; Path=/",)))
            s.route("GET", "/isosvc", lease_body(ROWS))
        await _run(_settings(a))
        await _run(_settings(b))
        assert b.hits("POST", "/api/logon")[0]["cookies"] == {}
        assert b.hits("GET", "/isosvc")[0]["cookies"] == {"SID": "B"}
    finally:
        a.close()
        b.close()


async def test_each_job_starts_with_an_empty_jar(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", lease_body(ROWS))
    await _run(_settings(srv))
    await _run(_settings(srv))
    assert [r["cookies"] for r in srv.hits("POST", "/api/logon")] == [{}, {}]


# ── Token ───────────────────────────────────────────────────────────────────

async def test_token_mode_uses_the_configured_path_and_header(srv) -> None:
    srv.route("POST", "/api/logon", json_resp({"data": {"token": "tok-123"}}))
    srv.route("GET", "/isosvc", lease_body(ROWS))
    await _run(_settings(srv, auth_mode="token", token_path="data.token"))
    assert srv.hits("GET", "/isosvc")[0]["headers"]["authorization"] == "Bearer tok-123"


async def test_token_mode_custom_header_without_prefix(srv) -> None:
    srv.route("POST", "/api/logon", json_resp({"t": "tok-9"}))
    srv.route("GET", "/isosvc", lease_body(ROWS))
    await _run(_settings(srv, auth_mode="token", token_path="t", token_header="X-Auth-Token", token_prefix=""))
    req = srv.hits("GET", "/isosvc")[0]
    assert req["headers"]["x-auth-token"] == "tok-9" and "authorization" not in req["headers"]


@pytest.mark.parametrize("body", [{"data": {}}, {"data": {"token": ""}}, {"token": "x"}, []])
async def test_missing_token_fails_without_guessing(srv, body) -> None:
    srv.route("POST", "/api/logon", json_resp(body))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv, auth_mode="token", token_path="data.token"))
    assert ei.value.spec_code == "AUTH_RESPONSE_UNSUPPORTED"
    assert not srv.hits("GET", "/isosvc")


# ── 讀租約：到期、權限、重試 ──────────────────────────────────────────────────

async def test_expired_session_relogs_in_once(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", [(401, [], b""), lease_body(ROWS)])
    got = await _run(_settings(srv))
    assert got.status == 200
    assert len(srv.hits("POST", "/api/logon")) == 2 and len(srv.hits("GET", "/isosvc")) == 2


async def test_relogin_is_capped_at_one(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", (401, [], b""))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "AUTH_FAILED" and ei.value.stage == "fetch"
    assert len(srv.hits("POST", "/api/logon")) == 2 and len(srv.hits("GET", "/isosvc")) == 2


async def test_lease_403_is_permission_denied_without_relogin(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", (403, [], b""))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "PERMISSION_DENIED"
    assert len(srv.hits("POST", "/api/logon")) == 1


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_gateway_errors_are_retried_once(srv, status) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", [(status, [], b""), lease_body(ROWS)])
    assert (await _run(_settings(srv))).status == 200
    assert len(srv.hits("GET", "/isosvc")) == 2


async def test_gateway_error_retry_is_capped(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", (503, [], b""))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "SERVER_ERROR"
    assert len(srv.hits("GET", "/isosvc")) == 2


async def test_lease_429_is_not_retried(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", (429, [("Retry-After", "30")], b""))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "RATE_LIMITED" and ei.value.retry_after == 30
    assert len(srv.hits("GET", "/isosvc")) == 1


async def test_lease_redirect_is_not_followed(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", (302, [("Location", "/login")], b""))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv))
    assert ei.value.spec_code == "UNEXPECTED_REDIRECT"
    assert not srv.hits("GET", "/login")


async def test_read_timeout_is_retried_once_then_fails(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", lease_body(ROWS))
    srv.delay[("GET", "/isosvc")] = 1.5
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv, request_timeout=0.4))
    assert ei.value.spec_code == "READ_TIMEOUT"
    assert len(srv.hits("GET", "/isosvc")) == 2


async def test_login_timeout_is_not_resent(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.delay[("POST", "/api/logon")] = 1.5
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv, request_timeout=0.4))
    assert ei.value.spec_code == "READ_TIMEOUT" and ei.value.stage == "login"
    assert len(srv.hits("POST", "/api/logon")) == 1


async def test_response_size_limit_aborts(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    srv.route("GET", "/isosvc", lease_body(ROWS * 500))
    with pytest.raises(IsoError) as ei:
        await _run(_settings(srv, max_bytes=2048))
    assert ei.value.spec_code == "RESPONSE_LIMIT_EXCEEDED"


async def test_connection_refused_is_connect_failed() -> None:
    s = MockServer()
    url = s.url
    s.close()
    st = C.Settings(base_url=url, login_path="/api/logon", login_method="POST", post_format="form",
                    username="u", password="p", connect_timeout=1.0, request_timeout=1.0)
    with pytest.raises(IsoError) as ei:
        async with C.IsoClient(st, check=_allow_loopback) as cli:
            await cli.login()
    assert ei.value.spec_code == "CONNECT_FAILED"


async def test_the_default_outbound_policy_still_blocks_loopback(srv) -> None:
    srv.route("POST", "/api/logon", _login_ok())
    with pytest.raises(IsoError) as ei:
        async with C.IsoClient(_settings(srv)) as cli:
            await cli.login()
    assert ei.value.spec_code == "OUTBOUND_BLOCKED"
    assert not srv.requests


# ── 秘密不外洩 ──────────────────────────────────────────────────────────────

async def test_secrets_never_reach_logs_errors_or_the_trace(srv, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    srv.route("GET", "/api/logon", _login_ok(cookies=("SID=cookie-value-1; Path=/",)))
    srv.route("GET", "/isosvc", (403, [], b""))
    trace = None
    with pytest.raises(IsoError) as ei:
        async with C.IsoClient(_settings(srv, login_method="GET"), check=_allow_loopback) as cli:
            try:
                await cli.login()
                await cli.fetch_leases()
            finally:
                trace = cli.trace
    from urllib.parse import quote, quote_plus
    forms = {PW, quote_plus(PW), quote(PW, safe=""), "cookie-value-1"}
    blob = caplog.text + str(ei.value) + json.dumps(ei.value.params, default=str) + json.dumps(trace, default=str)
    for f in forms:
        assert f not in blob, f"secret form {f!r} leaked"
    assert "/api/logon?<redacted>" in caplog.text or "/api/logon" in json.dumps(trace)
    # 診斷記錄可以有 Cookie 的名稱（真機驗收要問），但沒有值
    assert any("SID" in (st.get("cookies") or []) for st in trace)


async def test_token_value_never_reaches_the_trace(srv) -> None:
    srv.route("POST", "/api/logon", json_resp({"data": {"token": "very-secret-token"}}))
    srv.route("GET", "/isosvc", lease_body(ROWS))
    async with C.IsoClient(_settings(srv, auth_mode="token", token_path="data.token"),
                           check=_allow_loopback) as cli:
        await cli.login()
        await cli.fetch_leases()
        assert "very-secret-token" not in json.dumps(cli.trace, default=str)


async def test_anonymous_probe_sends_no_credentials(srv) -> None:
    srv.route("GET", "/isosvc", lease_body(ROWS))
    got = await C.anonymous_probe(_settings(srv), check=_allow_loopback)
    assert got["status"] == 200 and got["readable"] is True
    (req,) = srv.requests
    assert req["cookies"] == {} and "authorization" not in req["headers"]


# ── TLS ─────────────────────────────────────────────────────────────────────

def _self_signed_context(tmp_path):
    import datetime as _dt
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "isoinsight-test")])
    now = _dt.datetime.now(_dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_p, key_p = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2      # 測試靶也不開舊版 TLS（CodeQL #49）
    ctx.load_cert_chain(cert_p, key_p)
    return ctx


async def test_tls_verification_failure_is_its_own_code(tmp_path) -> None:
    s = MockServer(ssl_context=_self_signed_context(tmp_path))
    try:
        s.route("POST", "/api/logon", _login_ok(cookies=("SID=s1; Path=/; Secure",)))
        s.route("GET", "/isosvc", lease_body(ROWS))
        with pytest.raises(IsoError) as ei:
            await _run(_settings(s))
        assert ei.value.spec_code == "TLS_VERIFY_FAILED"
        # 管理員明確關掉驗證：登入、讀租約都還是走 HTTPS（Secure Cookie 也送得出去）
        got = await _run(_settings(s, verify_tls=False))
        assert got.status == 200
        assert s.hits("GET", "/isosvc")[-1]["cookies"] == {"SID": "s1"}
    finally:
        s.close()
