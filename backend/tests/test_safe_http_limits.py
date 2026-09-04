"""出站請求的兩道保護：共用連線與回應大小上限。

由來（2026-09-04，MikroTik 整合的前置修正）：客戶的 MikroTik 是**主力路由器**
（CCR2004／CCR1072），拉資料不可以把它拖慢，而我們自己有兩個問題：

1. `safe_request()` 每呼叫一次就新建一個 client → **每支端點各做一次 TLS 握手**。
   CCR1072 是 Tile 架構（核多但單核弱，握手跑在單核上），一輪十個區段就白費十次。
2. 完全**沒有回應大小上限**。RouterOS 的 REST 沒有分頁也沒有 limit，一支 `/ip/route`
   在跑 BGP 的路由器上可能是上百萬列 —— 讀完再判斷就已經 OOM 了。
"""

from __future__ import annotations

import httpx
import pytest
from app.core.safe_http import ResponseTooLarge, safe_request


class _Transport(httpx.AsyncBaseTransport):
    """回固定內容的假傳輸層；記錄被建立幾個連線（以請求次數代替）。"""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self.status, content=self.body,
                              headers={"content-type": "application/json"})


@pytest.mark.anyio
async def test_oversized_response_is_aborted_with_a_readable_error() -> None:
    """超過上限要中止並說清楚，而不是把記憶體吃光。"""
    transport = _Transport(b"x" * 5000)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ResponseTooLarge) as exc:
            await safe_request("GET", "https://example.com/big",
                               client=client, max_bytes=1000)
    assert "1000" in str(exc.value), "訊息要講出上限是多少，否則使用者不知道要調什麼"


@pytest.mark.anyio
async def test_response_within_the_limit_is_returned_intact() -> None:
    """沒超過就要跟平常一模一樣（含 .json()）——不能為了設限而改變行為。"""
    transport = _Transport(b'{"ok": true, "rows": [1, 2, 3]}')
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await safe_request("GET", "https://example.com/small",
                                  client=client, max_bytes=1_000_000)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "rows": [1, 2, 3]}


@pytest.mark.anyio
async def test_no_limit_means_no_streaming_path() -> None:
    """沒給上限時維持原本的行為（既有整合不受影響）。"""
    transport = _Transport(b'{"ok": true}')
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await safe_request("GET", "https://example.com/x", client=client)
    assert resp.json() == {"ok": True}


@pytest.mark.anyio
async def test_a_shared_client_is_reused_across_requests() -> None:
    """共用連線：多支端點只用同一個 client（TLS 握手因此只做一次）。

    這裡驗的是「傳進去的 client 真的被用到」——沒有被忽略、也沒有偷偷另建一個。
    """
    transport = _Transport(b"{}")
    async with httpx.AsyncClient(transport=transport) as client:
        for path in ("/a", "/b", "/c"):
            await safe_request("GET", f"https://example.com{path}", client=client)
    assert transport.calls == 3, "三次請求都應該走同一個 client"
