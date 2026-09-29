"""CodeQL 標出的 SSRF（2026-09-29）：工具頁的 HTTP 檢查讓任何登入的人叫伺服器去連任意網址。

內網是這個診斷工具本來的用途（不擋）；本機、link-local（雲端中繼資料 169.254.169.254）、多播不是診斷對象。
轉址後的每一跳都要檢查 —— 對外的網址可以 302 到 127.0.0.1。
"""
from __future__ import annotations

import pytest
from app.services import netdiag


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/api/v1/system/version",
    "http://localhost/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://0.0.0.0/",
])
async def test_loopback_and_metadata_are_refused(url) -> None:
    res = await netdiag.http_check(url)
    assert res.ok is False
    assert res.status is None
    assert "not a diagnostic target" in (res.error or "")


async def test_a_redirect_into_loopback_is_refused(monkeypatch) -> None:
    """對外的網址 302 到本機：第二跳要被擋，不可以真的去連。"""
    import httpx
    hits: list[str] = []

    async def fake_get(self, url, **kw):
        hits.append(str(url))
        req = httpx.Request("GET", url)
        return httpx.Response(302, headers={"location": "http://127.0.0.1:6379/"}, request=req)

    from urllib.parse import urlsplit

    async def ok_target(url):
        if urlsplit(url).hostname == "127.0.0.1":
            raise netdiag.DiagTargetBlocked("127.0.0.1 is loopback / link-local / multicast — not a diagnostic target")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(netdiag, "_assert_diag_http_target", ok_target)
    res = await netdiag.http_check("http://203.0.113.10/")
    assert hits == ["http://203.0.113.10/"], "轉址到本機的那一跳不可以送出"
    assert "not a diagnostic target" in (res.error or "")


async def test_private_addresses_are_still_allowed(monkeypatch) -> None:
    """內網主機是這個工具的正常用途。"""
    await netdiag._assert_diag_http_target("http://198.51.100.20/")
    await netdiag._assert_diag_http_target("http://10.20.0.12:8080/")
