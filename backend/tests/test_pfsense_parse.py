"""pfSense 同步的兩個歷史 DataError 回歸測試（客戶回報，已於 v0.5.48 修）：

1. 別名 descr 可能是 list → 存進 Text 欄前要用 _as_text 攤平成字串。
2. 規則/NAT 的 target 可能是別名名稱（如 Web_Test）→ _valid_ip 要回 None，
   才不會拿去 `WHERE ip_addresses.ip = 'Web_Test'` 觸發 asyncpg DataError。
"""
from __future__ import annotations

from app.services.pfsense import _as_text, _valid_ip


def test_as_text_flattens_list():
    assert _as_text(["Entry added Tue, 18 Nov 2014", "note"]) == "Entry added Tue, 18 Nov 2014; note"
    assert _as_text("plain") == "plain"
    assert _as_text(None) is None
    assert _as_text([]) is None
    assert _as_text([None, ""]) is None   # 全空 → None（不塞空字串進 Text）


def test_valid_ip_rejects_alias_names():
    # 別名名稱不是 IP → None（絕不拿去查 ip_addresses.ip）
    assert _valid_ip("Web_Test") is None
    assert _valid_ip("bogons") is None
    assert _valid_ip("") is None
    assert _valid_ip(None) is None
    # 合法 IP（含去首碼）才回值
    assert _valid_ip("10.20.30.40") == "10.20.30.40"
    assert _valid_ip("10.20.30.40/24") == "10.20.30.40"
    assert _valid_ip("2001:db8::5") == "2001:db8::5"


def test_rest_api_package_missing_gets_a_plain_message() -> None:
    """pfSense 升級會移除非官方套件：整個 /api/v2 變成 webConfigurator 的 404 HTML 頁（10-23 正式站台的一台 pfSense）。
    錯誤要直接講「REST API 套件不在、要重裝」，不是貼一段 HTML。"""
    from app.services.pfsense import _status_error
    msg = _status_error("/api/v2/diagnostics/arp_table", 404, "text/html; charset=UTF-8", "<!DOCTYPE html><html lang=\"en\">")
    assert "REST API" in msg
    assert "<html" not in msg.lower()
    # JSON 的 404（套件在，只是端點不存在）照舊帶原文
    msg2 = _status_error("/api/v2/foo", 404, "application/json", '{"code":404,"message":"Endpoint not found"}')
    assert "Endpoint not found" in msg2
