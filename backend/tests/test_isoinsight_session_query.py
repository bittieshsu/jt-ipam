"""ISOinsight：原廠說明的呼叫方式（2026-10-12 原廠回信）。

POST /logon（JSON：username、password）→ 回 {"SessionID": "...", "code": "200"}，
再以 GET /isosvc?act=DhcpLease&sessionid=<SessionID> 讀租約。

以前 Token 模式只會把 token 放在標頭，照原廠的方式設定也接不上。這裡驗：
- token 可以放在網址參數（名稱可設定），不送標頭
- 網址裡的 session ID 不進連線記錄、httpx 日誌、錯誤訊息（它跟密碼一樣能拿來讀資料）
- 登入回 HTTP 200 但 code 不是 200、又沒有 token：是登入失敗，不是「回應格式不支援」
- session 到期（401）重新登入一次，第二次帶新的 session ID
"""
from __future__ import annotations

import json
import logging
import uuid

import pytest
from app.services.isoinsight import client as C
from app.services.isoinsight.errors import IsoError

from tests.isoinsight_mock import MockServer, json_resp, lease_body

pytestmark = pytest.mark.anyio

USER, PW = "ipam-reader", "s3cret-pass"
SID1, SID2 = "0123456789ABCDEF0123456789ABCDEF", "FEDCBA9876543210FEDCBA9876543210"
ROWS = [{"ip": "192.0.2.20", "mac": "02:00:5e:00:53:20", "name": "laptop-07",
         "start_time": "2026/10/12 02:05:03", "end_time": "2026/10/12 06:05:03"}]


def _allow_loopback(_host, _addrs) -> None:
    """測試伺服器在 127.0.0.1：正式的出站政策一律擋迴路位址。"""


@pytest.fixture
def srv():
    s = MockServer()
    yield s
    s.close()


def _settings(srv: MockServer, **kw) -> C.Settings:
    base = {"base_url": srv.url, "login_path": "/logon", "login_method": "POST", "post_format": "json",
            "username": USER, "password": PW, "auth_mode": "token", "token_path": "SessionID",
            "token_in": "query", "token_param": "sessionid", "lease_path": "/isosvc?act=DhcpLease",
            "connect_timeout": 2.0, "request_timeout": 2.0}
    base.update(kw)
    return C.Settings(**base)


def _logon(sids):  # type: ignore[no-untyped-def]
    it = iter(sids)

    def h(req):  # type: ignore[no-untyped-def]
        body = json.loads(req["body"] or b"{}")
        if body != {"username": USER, "password": PW}:
            return json_resp({"code": "401"})
        return json_resp({"SessionID": next(it), "code": "200"})
    return h


def _leases_for(valid):  # type: ignore[no-untyped-def]
    def h(req):  # type: ignore[no-untyped-def]
        if req["params"].get("sessionid") != [valid]:
            return 401, [], b""
        return lease_body(ROWS)
    return h


async def test_vendor_flow_session_id_in_the_query(srv) -> None:
    srv.route("POST", "/logon", _logon([SID1]))
    srv.route("GET", "/isosvc", _leases_for(SID1))
    async with C.IsoClient(_settings(srv), check=_allow_loopback) as cli:
        await cli.login()
        got = await cli.fetch_leases()
    assert got.status == 200
    (req,) = srv.hits("GET", "/isosvc")
    assert req["params"] == {"act": ["DhcpLease"], "sessionid": [SID1]}
    assert "authorization" not in req["headers"]
    (login,) = srv.hits("POST", "/logon")
    assert login["headers"]["content-type"].startswith("application/json")


async def test_session_id_never_reaches_logs_errors_or_the_trace(srv, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    srv.route("POST", "/logon", _logon([SID1]))
    srv.route("GET", "/isosvc", (403, [], b""))
    trace = None
    with pytest.raises(IsoError) as ei:
        async with C.IsoClient(_settings(srv), check=_allow_loopback) as cli:
            try:
                await cli.login()
                await cli.fetch_leases()
            finally:
                trace = cli.trace
    blob = caplog.text + str(ei.value) + json.dumps(ei.value.params, default=str) + json.dumps(trace, default=str)
    assert SID1 not in blob
    assert PW not in blob
    fetch = [t for t in trace if t.get("stage") == "fetch"]
    assert fetch
    assert "sessionid=<redacted>" in fetch[0]["path"]
    assert "act=DhcpLease" in fetch[0]["path"], "管理員設的固定參數照樣顯示，只遮 session ID"


async def test_login_code_other_than_200_is_auth_failed(srv) -> None:
    """帳密錯時設備仍回 HTTP 200，只是 code 不是 200、沒有 SessionID。"""
    srv.route("POST", "/logon", _logon([SID1]))
    with pytest.raises(IsoError) as ei:
        async with C.IsoClient(_settings(srv, password="wrong"), check=_allow_loopback) as cli:
            await cli.login()
    assert ei.value.spec_code == "AUTH_FAILED"
    assert "401" in str(ei.value)
    assert not srv.hits("GET", "/isosvc")


async def test_expired_session_relogs_in_with_the_new_id(srv) -> None:
    srv.route("POST", "/logon", _logon([SID1, SID2]))
    srv.route("GET", "/isosvc", _leases_for(SID2))     # 第一個 session 已經失效
    async with C.IsoClient(_settings(srv), check=_allow_loopback) as cli:
        await cli.login()
        got = await cli.fetch_leases()
    assert got.status == 200
    assert [r["params"]["sessionid"] for r in srv.hits("GET", "/isosvc")] == [[SID1], [SID2]]


async def test_header_mode_is_unchanged(srv) -> None:
    srv.route("POST", "/logon", json_resp({"SessionID": SID1, "code": "200"}))
    srv.route("GET", "/isosvc", lease_body(ROWS))
    async with C.IsoClient(_settings(srv, token_in="header", token_header="X-Session", token_prefix=""),
                           check=_allow_loopback) as cli:
        await cli.login()
        await cli.fetch_leases()
    (req,) = srv.hits("GET", "/isosvc")
    assert req["headers"]["x-session"] == SID1
    assert "sessionid" not in req["params"]


# ─────────────────── API：設定存得進去、讀得回來 ───────────────────
async def test_api_keeps_the_token_placement(client, auth_headers, db_session) -> None:
    from tests.test_isoinsight_api import _subnet
    sub = await _subnet(db_session)
    await db_session.commit()
    body = {"name": f"iso-{uuid.uuid4().hex[:6]}", "base_url": "https://192.0.2.10", "login_path": "/logon",
            "login_method": "POST", "post_format": "json", "username": USER, "password": PW,
            "auth_mode": "token", "token_path": "SessionID", "token_in": "query", "token_param": "sessionid",
            "timezone_confirmed": True, "scope_subnet_ids": [str(sub.id)]}
    r = await client.post("/api/v1/isoinsight/sources", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    got = r.json()
    assert (got["token_in"], got["token_param"]) == ("query", "sessionid")
    bad = await client.post("/api/v1/isoinsight/sources", headers=auth_headers,
                            json={**body, "name": "iso-bad", "token_param": "session id&x=1"})
    assert bad.status_code == 422
    r2 = await client.patch(f"/api/v1/isoinsight/sources/{got['id']}", headers=auth_headers,
                            json={"token_in": "header"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["token_in"] == "header"
