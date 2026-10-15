"""AI 對話的上下文預算（2026-10-15 事故）。

三個子代理補齊工具欄位時順手把說明寫長：工具清單從 4.7 萬字長到 6.4 萬字。正式站台 num_ctx=16384，
Ollama 實測提示詞 16,231 token，只剩 153 個 token 可以回答 —— gemma 開始空白回答（不是每次，所以
「有時候好、有時候壞」），畫面卻顯示「查詢步驟過多仍未完成」：模型一個工具都沒叫，這段話完全誤導。

三道防線：
1. 工具清單有大小上限（這支測試）—— 說明變長會被擋下來，而不是到客戶那邊才靜靜壞掉
2. 空白回答講真正的原因：提示詞用掉幾個 token／上限多少（Ollama 會回 prompt_eval_count）
3. 「強制作答」失敗時不再冒充成步驟過多 —— 以前補救函式永遠回一段非空字串，
   `retry or _empty_answer_message(...)` 的後半段從來沒有機會顯示
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.services import ai as ai_mod

pytestmark = pytest.mark.anyio

#: 工具清單 JSON 的字元數上限。2026-10-15 精簡後約 4.8 萬字（Ollama 實測整個提示詞約 12,250 token）；
#: 正式站台 num_ctx 16384，還要留給工具結果與回答。要放寬前先量 prompt_eval_count。
TOOL_SCHEMA_BUDGET = 50_000
#: 單一工具說明的上限：說明不是欄位清單，欄位在回傳的資料裡
DESCRIPTION_BUDGET = 520


def test_tool_list_fits_the_context_budget() -> None:
    from app.mcp.tools import TOOLS
    tools, _convo = ai_mod._build_chat_context([{"role": "user", "content": "x"}], "zh-TW", None, None)
    size = len(json.dumps(tools, ensure_ascii=False))
    assert size <= TOOL_SCHEMA_BUDGET, (
        f"工具清單 {size} 字，超過 {TOOL_SCHEMA_BUDGET}：num_ctx 16384 的站台會開始空白回答。"
        "精簡說明（欄位不用逐一列出）或先量 prompt_eval_count 再調整上限")
    long = {n: len(m["description"]) for n, m in TOOLS.items() if len(m["description"]) > DESCRIPTION_BUDGET}
    assert not long, f"工具說明太長（上限 {DESCRIPTION_BUDGET} 字）：{long}"


def _ndjson(*chunks: dict[str, Any]) -> list[str]:
    return [json.dumps(c, ensure_ascii=False) for c in chunks]


class _Resp:
    def __init__(self, lines: list[str]):
        self.status_code = 200
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _PostResp:
    status_code = 200

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


async def _run(monkeypatch, db_session, user, *, prompt_tokens: int | None, locale: str = "zh-TW",
               num_ctx: int | None = 16384) -> str:
    from app.services.system_config import LLMConfig
    cfg = LLMConfig(enabled=True, url="http://llm:11434", embedding_model="e", chat_model="m",
                    timeout=30.0, provider="ollama", num_ctx=num_ctx)

    async def _cfg(_s):
        return cfg
    monkeypatch.setattr("app.services.system_config.get_llm_config", _cfg)
    final = {"message": {"content": ""}, "done": True, "done_reason": "stop"}
    if prompt_tokens is not None:
        final["prompt_eval_count"] = prompt_tokens

    @asynccontextmanager
    async def _stream(method, url, *, headers=None, json=None, timeout=None):
        yield _Resp(_ndjson(final))
    monkeypatch.setattr(ai_mod, "safe_stream", _stream)

    async def _post(url, body, headers, timeout):
        return _PostResp({"message": {"content": ""}, "done": True})       # 強制作答也是空的
    monkeypatch.setattr(ai_mod, "_post_chat", _post)
    events = [ev async for ev in ai_mod.chat_stream(
        db_session, user=user, messages=[{"role": "user", "content": "電路的到期日在哪裡查？"}], locale=locale)]
    return next(e for e in events if e.get("type") == "done")["answer"]


async def test_a_prompt_that_fills_num_ctx_is_named(monkeypatch, db_session, admin_user) -> None:
    ans = await _run(monkeypatch, db_session, admin_user, prompt_tokens=16231)
    assert "16231" in ans and "16384" in ans, ans
    assert "查詢步驟過多" not in ans, "模型一個工具都沒叫，不可以說成步驟過多"


async def test_an_empty_answer_is_explained_not_called_too_many_steps(monkeypatch, db_session, admin_user) -> None:
    ans = await _run(monkeypatch, db_session, admin_user, prompt_tokens=3000)
    assert "查詢步驟過多" not in ans
    assert "num_ctx" in ans


async def test_the_explanation_follows_the_users_language(monkeypatch, db_session, admin_user) -> None:
    ja = await _run(monkeypatch, db_session, admin_user, prompt_tokens=16300, locale="ja-JP")
    assert "16300" in ja and "トークン" in ja, ja
    en = await _run(monkeypatch, db_session, admin_user, prompt_tokens=None, locale="en-US")
    assert en.startswith("No answer was produced")


def test_prompt_tokens_are_read_from_both_stream_formats() -> None:
    assert ai_mod._stream_delta({"done": True, "prompt_eval_count": 1234}).prompt_tokens == 1234
    oa = ai_mod._stream_delta({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 99}})
    assert oa.prompt_tokens == 99
