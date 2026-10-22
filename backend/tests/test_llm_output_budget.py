"""模型輸出上限依 LLM 設定自動計算（2026-10-21 使用者：「要配合 LLM 設定自動跟著變，不要固定 ——
不同公司用的 AI 主機與模型都不一樣」）。

起因：IP 變更評估寫死 2000 token，逐項解釋寫到一半被切斷、JSON 不完整、退回範本摘要。
規則：可用輸出＝上下文長度（判讀／巡檢自己的 num_ctx，沒設沿用對話模型的）－ 提示詞估計 － 保留；
上下文換大，額度就跟著變大。另外保留一個很高的防呆上限（模型偶爾卡在重複迴圈）。
"""
from __future__ import annotations

import pytest

from app.services import ai as ai_mod

pytestmark = pytest.mark.anyio


def test_budget_follows_the_context_length() -> None:
    small = ai_mod.output_budget("x" * 4000, 16384, "ollama")
    big = ai_mod.output_budget("x" * 4000, 131072, "ollama")
    assert 14000 < small < 16384                          # 16384 － 約 1000 － 保留
    assert big > small                                    # 上下文換大，額度跟著變大
    assert big <= ai_mod.OUTPUT_LOOP_GUARD
    cjk = ai_mod.output_budget("字" * 12000, 16384, "ollama")
    assert cjk < 16384 - 12000                            # 中文一字約一 token，不可低估
    assert ai_mod.output_budget("字" * 20000, 16384, "ollama") == ai_mod.OUTPUT_FLOOR


def test_unset_context_uses_the_server_default() -> None:
    assert ai_mod.output_budget("hi", None, "ollama") < 4096        # Ollama 沒設 num_ctx 時的預設
    assert ai_mod.output_budget("hi", None, "openai") > 0          # OpenAI 相容端點不知道上下文：給保守值


async def test_raw_chat_auto_budget_is_sent_as_num_predict(monkeypatch, db_session) -> None:
    sent: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"message": {"content": "ok"}}

    async def _fake_request(method, url, **kw):  # type: ignore[no-untyped-def]
        sent.update(kw["json"])
        return _Resp()

    monkeypatch.setattr(ai_mod, "safe_request", _fake_request)

    class _Cfg:
        provider = "ollama"
        api_key = None
        enabled = True
        url = "http://ollama.invalid:11434"
        chat_model = "m:1b"
        timeout = 30.0
        num_ctx = 32768

    async def _cfg(s):  # type: ignore[no-untyped-def]
        return _Cfg()
    from app.services import system_config
    monkeypatch.setattr(system_config, "get_llm_config", _cfg)
    await ai_mod.raw_chat(db_session, "hi", max_output_tokens="auto")
    assert sent["options"]["num_predict"] == ai_mod.output_budget("hi", 32768, "ollama")
    sent.clear()
    await ai_mod.raw_chat(db_session, "hi", max_output_tokens="auto", num_ctx=8192)   # 判讀自己的上下文優先
    assert sent["options"]["num_predict"] == ai_mod.output_budget("hi", 8192, "ollama")


def test_every_llm_feature_uses_the_automatic_budget() -> None:
    """守門：送進模型的地方不可以再寫死輸出上限（新功能也一樣）。"""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    bad = []
    for p in root.rglob("*.py"):
        if p.name == "ai.py" and p.parent.name == "services":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"max_output_tokens\s*=\s*(?!['\"]auto['\"])[A-Za-z0-9_]", line):
                bad.append(f"{p.relative_to(root)}:{i}: {line.strip()}")
    assert not bad, "寫死的輸出上限（改用 max_output_tokens=\"auto\"）：\n" + "\n".join(bad)
