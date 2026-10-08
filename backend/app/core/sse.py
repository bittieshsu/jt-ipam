"""SSE 串流的保活（使用者 2026-10-08：AI 對話「怎麼跑那麼久，然後沒出來？」畫面 network error）。

模型查工具、想答案，或路徑追蹤等一個沒回應的躍點時，串流可能幾十秒一個位元組都沒送出去。
反向代理的讀取逾時（本產品的 nginx 設定 /api/ 是 30 秒，客戶自己的代理多半 60 秒）一到就把連線切掉，
後端其實還在算，畫面只剩一句網路錯誤。

等待期間每隔幾秒送一則 SSE 註解（冒號開頭的行）：任何代理都看得到有資料在流動；
瀏覽器端的解析只讀 `data:` 開頭的行，註解會被略過。這樣不必要求客戶改代理的逾時設定。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

#: 多久沒有事件就送一則保活註解（秒）；要明顯小於 30 秒
KEEPALIVE_SECONDS = 10.0
#: SSE 註解；前端解析只讀 data: 開頭的行
KEEPALIVE = ": keepalive\n\n"


async def with_keepalive(agen: AsyncIterator[Any], interval: float | None = None) -> AsyncIterator[Any | None]:
    """照順序轉出 agen 的事件；超過 interval 秒沒有新事件就先給一個 None（呼叫端換成 KEEPALIVE）。

    事件在背景任務裡收，但**一次只拿一個**：呼叫端處理完上一個事件（例如寫對話紀錄）之後，
    背景任務才去拿下一個 —— 兩邊常共用同一個資料庫 session，不可以同時操作。
    agen 拋出的例外原樣交給呼叫端；呼叫端停止（使用者關掉畫面、斷線）時，agen 會被收掉，
    不會留在背景繼續呼叫模型或外部指令。
    """
    wait = KEEPALIVE_SECONDS if interval is None else interval
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1)
    go = asyncio.Event()

    async def pump() -> None:
        try:
            async for ev in agen:
                await queue.put(("ev", ev))
                await go.wait()
                go.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(("err", exc))
            return
        await queue.put(("end", None))

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                kind, val = await asyncio.wait_for(queue.get(), timeout=wait)
            except TimeoutError:
                yield None
                continue
            if kind == "ev":
                yield val
                go.set()
            elif kind == "err":
                raise val
            else:
                return
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
