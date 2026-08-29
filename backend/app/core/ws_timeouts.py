"""WebSocket 等待的時限。

主控台的 WebSocket 有兩種等待，處理方式必須不同：

**閒置等下一個指令** —— 終端機開著沒人打字是常態，等好幾個小時完全正常。
這種地方**不可以**加逾時，加了就是把正常的連線砍掉。

**協定進行到一半在等對方** —— 伺服器已經承諾了一個多步驟交換（等連線設定、等使用者
回答是否信任主機金鑰、等上傳的資料框），對方不接話就會一直卡著。這種地方**一定要**
有時限：少了它，只要開 N 條連線然後不說話，就能佔住 N 個協程與其後的連線資源，
而且從外面看起來只是「系統很慢」。

實機上就踩過：SFTP 上傳在 `put_ready` 之後沒收到資料框，伺服器無限期等待，
使用者看到的是「連線已中斷」加上之後所有請求逾時。
"""

from __future__ import annotations

import asyncio
from typing import Any

#: 連上來之後，等第一個連線設定訊息的時限（秒）。正常客戶端是立刻送出。
HANDSHAKE_TIMEOUT = 30

#: 等使用者回答「是否信任這把主機金鑰」的時限（秒）。要留給人讀完訊息再決定，
#: 但不能是無限 —— 那等於把連線資源交給對方決定何時釋放。
PROMPT_TIMEOUT = 180


class WsTimeout(TimeoutError):
    """協定中途等待逾時；帶著人看得懂的說明，好讓前端直接顯示。"""


async def receive_text_within(websocket: Any, timeout: float, *, what: str) -> str:
    """在時限內收一則文字訊息，逾時丟 `WsTimeout`。

    `what` 是要顯示給人看的描述（「連線設定」「主機金鑰確認」），
    因為使用者看到的錯誤如果只寫 timeout，等於沒說。
    """
    try:
        return await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
    except TimeoutError as exc:
        raise WsTimeout(f"等待{what}超過 {int(timeout)} 秒，連線已關閉") from exc
