"""ISOinsight 整合：來源設定的驗證與遮蔽（純函式）。"""
from __future__ import annotations

import pytest
from app.services.isoinsight import config as C


@pytest.mark.parametrize(("raw", "want"), [
    ("https://192.0.2.10", "https://192.0.2.10"),
    ("https://192.0.2.10/", "https://192.0.2.10"),
    ("http://192.0.2.10:8080/", "http://192.0.2.10:8080"),
    ("  https://iso.example.test/portal/ ", "https://iso.example.test/portal"),
    ("https://[2001:db8::10]:8443", "https://[2001:db8::10]:8443"),
])
def test_base_url_is_normalised(raw: str, want: str) -> None:
    assert C.normalize_base_url(raw) == want


@pytest.mark.parametrize("raw", [
    "ftp://192.0.2.10", "192.0.2.10", "https://", "https://user:pw@192.0.2.10",
    "https://192.0.2.10/?a=1", "https://192.0.2.10/#frag", "https://192.0.2.10:99999",
    "https://192.0.2.10/a b", "javascript:alert(1)",
])
def test_bad_base_urls_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        C.normalize_base_url(raw)


@pytest.mark.parametrize("raw", ["/api/logon", "/api/v2/logon", "/x-y_z.cgi"])
def test_login_path_is_a_same_host_relative_path(raw: str) -> None:
    assert C.normalize_path(raw, allow_query=False) == raw


@pytest.mark.parametrize("raw", [
    "api/logon", "//evil.example/x", "https://evil.example/x", "/a/../b", "/a#b", "/a\\b",
    "/api/logon?username=x", "/a b", "",
])
def test_login_path_rejects_cross_host_and_query(raw: str) -> None:
    with pytest.raises(ValueError):
        C.normalize_path(raw, allow_query=False)


def test_lease_path_keeps_its_query_and_splits_it() -> None:
    p = C.normalize_path("/isosvc?act=DhcpLease", allow_query=True)
    assert p == "/isosvc?act=DhcpLease"
    path, params = C.split_path(p)
    assert path == "/isosvc" and params == [("act", "DhcpLease")]


def test_build_url_joins_base_prefix_and_path() -> None:
    assert C.join_url("https://192.0.2.10/portal", "/api/logon") == "https://192.0.2.10/portal/api/logon"


@pytest.mark.parametrize("name", ["username", "user_name", "login.id", "pw[0]"])
def test_param_names(name: str) -> None:
    assert C.check_param_name(name) == name


@pytest.mark.parametrize("name", ["", "a b", "a=b", "a&b", "x" * 65])
def test_bad_param_names(name: str) -> None:
    with pytest.raises(ValueError):
        C.check_param_name(name)


def test_token_path_extraction_is_explicit() -> None:
    assert C.extract_token({"data": {"token": "abc"}}, "data.token") == "abc"
    assert C.extract_token({"items": [{"t": "z"}]}, "items.0.t") == "z"
    for body in ({"data": {}}, {"data": {"token": ""}}, {"data": {"token": True}}, {"data": {"token": 1.5}},
                 [], {"data": None}, {"token": "x"}):
        assert C.extract_token(body, "data.token") is None


def test_numeric_session_id_is_accepted() -> None:
    """Session ID 是整數也算（很常見）；布林與小數不算。客戶 2026-10-13：登入 HTTP 200、設定的路徑卻取不到值。"""
    assert C.extract_token({"code": 200, "SessionID": 1234567}, "SessionID") == "1234567"


def test_key_case_is_forgiven_only_when_unambiguous() -> None:
    """同一層只有一個欄位是大小寫不同的同名欄位時採用它，並回報實際的路徑；兩個以上就不猜。"""
    assert C.extract_token_ex({"data": {"sessionId": "abc"}}, "data.SessionID") == ("abc", "data.sessionId")
    assert C.extract_token_ex({"SessionID": "abc"}, "SessionID") == ("abc", "SessionID")
    assert C.extract_token_ex({"sessionid": "a", "SESSIONID": "b"}, "SessionID") == (None, None)


def test_response_shape_lists_keys_and_types_not_values() -> None:
    """登入回應取不到 Token 時給人看的結構：欄位與型別、字串只給長度；code／msg 這類狀態欄位才給值。"""
    shape = C.describe_shape({"code": 200, "msg": "ok", "data": {"sessionId": "s" * 32, "n": 3, "on": True,
                                                                  "items": [1, 2], "x": None}})
    assert shape == '{code: 200, msg: "ok", data: {sessionId: str(32), n: int, on: bool, items: [2 items], x: null}}'
    assert "sss" not in shape
    assert C.describe_shape({"code": 401, "msg": "wrong password"}) == '{code: 401, msg: "wrong password"}'
    assert C.describe_shape([{"a": 1}]) == "[1 items]"
    assert len(C.describe_shape({f"k{i}": "v" for i in range(200)})) <= 400


@pytest.mark.parametrize("bad", ["", "a..b", ".a", "a b", "a." + "b." * 12])
def test_bad_token_paths(bad: str) -> None:
    with pytest.raises(ValueError):
        C.check_token_path(bad)


@pytest.mark.parametrize("bad", ["Host", "content-length", "Cookie", "a b", "X:Y", ""])
def test_token_header_cannot_be_a_framing_or_cookie_header(bad: str) -> None:
    with pytest.raises(ValueError):
        C.check_header_name(bad)


def test_timezone_must_exist() -> None:
    assert C.check_timezone("Asia/Taipei") == "Asia/Taipei"
    with pytest.raises(ValueError):
        C.check_timezone("Mars/Olympus")


def test_redact_url_drops_query_fragment_and_userinfo() -> None:
    url = "https://u:p@192.0.2.10:8443/api/logon?username=a&password=s3cr%26t#x"
    assert C.redact_url(url) == "https://192.0.2.10:8443/api/logon?<redacted>"
    assert C.redact_url("/api/logon?password=x") == "/api/logon?<redacted>"
    assert C.redact_url("/isosvc") == "/isosvc"


def test_scrub_removes_raw_and_encoded_secret_values() -> None:
    pw = "p a&s+s%w=rd?中"
    from urllib.parse import quote, quote_plus
    text = f"boom {pw} and {quote_plus(pw)} and {quote(pw, safe='')} Cookie: SID=abc"
    out = C.scrub(text, [pw])
    assert pw not in out and quote_plus(pw) not in out and quote(pw, safe="") not in out
    assert "***" in out


def test_scrub_masks_sensitive_header_lines() -> None:
    out = C.scrub("Set-Cookie: SID=abc; Path=/\nAuthorization: Bearer xyz\npassword=hunter2", [])
    assert "abc" not in out and "xyz" not in out and "hunter2" not in out
