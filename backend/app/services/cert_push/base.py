"""憑證推送的共用型別（見 models/cert_push_target.py 的說明）。

每家設備一個 driver（drivers.py），流程都是：
  讀目前設定（換回去要用）→ 匯入或替換憑證 → 換上 → （引擎）連過去確認對方出示的是新憑證 → 不是就換回去。
driver 只透過 Http 跟設備說話，測試把 Http 換成模擬設備（tests/test_cert_push.py）。
設備回的錯誤訊息是對方系統的文字，只當資料顯示，不拿來做判斷以外的事。
"""
from __future__ import annotations

import base64
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Bundle:
    """要推的那一版。key_pem 是解密後的明文，只在記憶體裡、推完就丟。"""
    cert_name: str
    fingerprint: str
    cert_pem: str
    chain_pem: str
    key_pem: str

    @property
    def fullchain(self) -> str:
        parts = [self.cert_pem.strip()]
        if self.chain_pem and self.chain_pem.strip():
            parts.append(self.chain_pem.strip())
        return "\n".join(parts) + "\n"

    def device_name(self, limit: int = 35) -> str:
        """設備上的憑證名稱：jtipam-<名稱>-<指紋前 8 碼>。同一版重推是同一個名字（可重複執行）。"""
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", self.cert_name).strip("-")[: max(1, limit - 16)] or "cert"
        return f"jtipam-{slug}-{self.fingerprint[:8]}"


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class PushError(Exception):
    """推送失敗。step＝在哪一步、code＝給畫面翻譯的代碼、detail＝設備回的原文（截短）。"""

    def __init__(self, step: str, code: str, detail: str = "") -> None:
        super().__init__(f"{step}: {code}: {detail}")
        self.step, self.code, self.detail = step, code, (detail or "")[:500]


class Response(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class Http(Protocol):
    async def __call__(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                       content: bytes | None = None, headers: dict[str, str] | None = None,
                       timeout: float = 30.0) -> Response: ...


@dataclass
class Steps:
    """做了什麼、在哪一步失敗；存進 last_steps 給畫面看。"""
    items: list[dict[str, Any]] = field(default_factory=list)

    def ok(self, step: str, detail: str = "") -> None:
        self.items.append({"step": step, "ok": True, "detail": detail[:300]})

    def fail(self, step: str, code: str, detail: str = "") -> None:
        self.items.append({"step": step, "ok": False, "code": code, "detail": detail[:300]})


Rollback = Callable[[], Awaitable[None]]


class Driver(Protocol):
    kind: str

    async def push(self, bundle: Bundle, steps: Steps) -> Rollback | None:
        """推上去並換上；回傳「換回去」的函式（引擎確認失敗時呼叫），沒有東西要換回就回 None。"""
        ...
