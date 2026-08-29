"""上傳中途沒有資料時，不可以讓整條連線卡死。

客戶回報：拖兩個檔案進去上傳 → 「連線已中斷」，遠端只出現一個 **0 位元組**的檔案，
之後要再連線時 ticket 請求逾時、整個畫面很久才有回應。

串起來就是同一件事：收檔案的迴圈是

    while written < size:
        chunk = await websocket.receive_bytes()

客戶端在 `put_ready` 之後只要沒把宣告的位元組送完（大檔被瀏覽器的送出緩衝擋下、
拖曳的檔案 handle 失效…），伺服器就**無限期**停在這裡。檔案已經被 open("wb") 建出來
所以遠端看得到 0 位元組；那條協程一直佔著 WebSocket 與 asyncssh 連線；使用者以為是
「連線壞了」，實際上是伺服器還在等一個永遠不會來的框。

修法是給每一個資料框一個逾時：逾時就回報錯誤、放掉那個檔案，**連線繼續可用**。
不是把整條連線關掉 —— 一次上傳失敗不該讓人重連。
"""

from __future__ import annotations

import inspect

from app.api.v1.endpoints import sftp_console


def test_upload_receive_has_a_stall_timeout():
    """收上傳資料的地方必須有逾時保護。

    這裡用原始碼檢查而不是端到端：要真的重現「客戶端連上來、宣告大小、然後不送」
    需要一台可連的 SSH 伺服器，而這個缺陷的本質是「那一行沒有逾時」——
    直接對著它斷言最不容易因為環境而失真。
    """
    src = inspect.getsource(sftp_console)
    put_block = src[src.index('elif op == "put":'):]
    put_block = put_block[:put_block.index('elif op == "mkdir":')]

    assert "receive_bytes()" in put_block, "測試對象改名了，請一起更新"
    assert "wait_for" in put_block or "timeout" in put_block, (
        "上傳的接收迴圈沒有逾時保護：客戶端不送資料時伺服器會無限期佔住這條連線"
    )


def test_stall_timeout_is_bounded_and_sane():
    """逾時值要看得見、而且不能長到跟沒有一樣。"""
    t = getattr(sftp_console, "UPLOAD_STALL_TIMEOUT", None)
    assert t is not None, "逾時值要是一個具名常數，別埋在程式碼裡"
    assert 5 <= t <= 120, f"逾時 {t}s 不合理：太短會誤殺慢速上傳，太長等於沒有保護"


def test_a_stalled_upload_does_not_close_the_session():
    """逾時之後要能繼續用同一條連線 —— 一次上傳失敗不該逼人重連。"""
    src = inspect.getsource(sftp_console)
    put_block = src[src.index('elif op == "put":'):]
    put_block = put_block[:put_block.index('elif op == "mkdir":')]
    # 逾時的處理要在迴圈內把錯誤送回去，而不是往外拋掉整條連線
    assert "put_stalled" in put_block or "continue" in put_block, (
        "逾時後應該回報錯誤並讓連線繼續，而不是讓例外往外炸掉整個 session"
    )
