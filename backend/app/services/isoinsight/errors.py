"""ISOinsight 整合的錯誤代碼。

兩層代碼：
- `spec_code`（大寫，例如 `AUTH_FAILED`）：存進同步記錄與來源的 `last_error_code`，給人與程式查詢
- `code`（UiError，`isoinsight_<小寫>`）：前端 `errors.<code>` 翻譯；`UI_CODES` 列出全部可能值，
  `tests/test_ui_error_codes.py` 會確認三個語系都翻得出來

**錯誤訊息（`str(exc)`）一律不可以含帳密、Cookie、Token 或帶 Query 的登入網址**：它會進同步記錄、
背景作業的錯誤欄、排程腳本的日誌。組訊息的地方都只放狀態碼、方法、已遮蔽的路徑與底層例外類別。
"""
from __future__ import annotations

from typing import Any

from app.core.ui_error import UiError

#: 全部的錯誤代碼（規格 §11 的清單＋實作上需要分得更細的幾個）
SPEC_CODES: tuple[str, ...] = (
    "CONNECT_TIMEOUT",            # 連線逾時
    "CONNECT_FAILED",             # 連線被拒、名稱解析不到、路由不通
    "READ_TIMEOUT",               # 讀取逾時
    "TLS_VERIFY_FAILED",          # 憑證驗證失敗
    "OUTBOUND_BLOCKED",           # 被出站連線政策擋下（本機、link-local、私網未允許…）
    "AUTH_FAILED",                # 登入失敗（401／403、登入後讀取仍未授權）
    "AUTH_FLOW_UNSUPPORTED",      # 需要尚未支援的登入流程（回 HTML 頁、CSRF、預備請求）
    "AUTH_RESPONSE_UNSUPPORTED",  # 登入回應無法按設定解析（沒有 Cookie、Token 欄位缺值）
    "PERMISSION_DENIED",          # 帳號無租約讀取權限（讀租約時 403）
    "METHOD_UNSUPPORTED",         # 405／415：方法或 POST 格式不相容
    "UNEXPECTED_REDIRECT",        # 收到未支援的重導向（初版不跟隨）
    "RATE_LIMITED",               # 429：交由排程延後
    "SERVER_ERROR",               # 5xx
    "HTTP_ERROR",                 # 其他非預期的 HTTP 狀態（例如 404：路徑不對）
    "INVALID_RESPONSE",           # 不是預期的租約 JSON
    "INCOMPLETE_RESPONSE",        # 回應帶著還沒取完的分頁資訊
    "RESPONSE_LIMIT_EXCEEDED",    # 回應大小或筆數超過上限
    "JOB_TIMEOUT",                # 整次工作超過時限
    "SUBNET_UNMAPPED",            # 無法唯一配對到允許的子網路（記錄層級，不是整次失敗）
    "SYNC_ALREADY_RUNNING",       # 這個來源已經有工作在跑
    "WRITE_FAILED",               # 同步交易失敗，既有資料保留
    "SOURCE_DISABLED",            # 來源已停用（工作途中被停用則不提交）
    "PREVIEW_REQUIRED",           # 排程開著，但還沒用目前的設定成功預覽過：排程不同步
    "CONFIG_CHANGED",             # 工作途中設定被改了，不提交
    "CONFIG_INVALID",             # 設定不完整（例如解不開密碼）
    "INTERNAL_ERROR",             # 意外的程式錯誤（記錄只帶例外類別，詳細在伺服器日誌）
)

UI_CODES: frozenset[str] = frozenset(f"isoinsight_{c.lower()}" for c in SPEC_CODES)

#: 可以在同一個工作裡重試一次的（網路類）
RETRYABLE_TRANSPORT = frozenset({"CONNECT_TIMEOUT", "CONNECT_FAILED", "READ_TIMEOUT"})


class IsoError(UiError):
    """ISOinsight 整合的錯誤。`stage`：login／fetch／validate／write…；`http_status`：有的話。"""

    def __init__(self, spec_code: str, message: str, *, stage: str, http_status: int | None = None,
                 retry_after: float | None = None, **params: Any) -> None:
        if spec_code not in SPEC_CODES:
            raise ValueError(f"unknown ISOinsight error code {spec_code!r}")
        super().__init__(message, code=f"isoinsight_{spec_code.lower()}",
                         stage=stage, status=http_status, **params)
        self.spec_code = spec_code
        self.stage = stage
        self.http_status = http_status
        self.retry_after = retry_after
