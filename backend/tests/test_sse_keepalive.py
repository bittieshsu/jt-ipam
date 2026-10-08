"""串流在等待時要送保活（使用者 2026-10-08：AI 對話「怎麼跑那麼久，然後沒出來？」畫面 network error）。

模型查工具、想答案，或路徑追蹤等沒回應的躍點時，一個位元組都沒送出去，
nginx 的 proxy_read_timeout（本產品設定 /api/ 是 30 秒）就把連線切掉，後端其實還在算。
等待期間每隔幾秒送一則 SSE 註解（`: keepalive`），任何反向代理都不會因為等太久而切斷；
前端只讀 `data:` 開頭的行，註解會被略過。三個串流端點都要有。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from app.core import sse
from app.services.system_config import LLMConfig


@pytest.fixture
def fast_keepalive(monkeypatch) -> None:
    monkeypatch.setattr(sse, "KEEPALIVE_SECONDS", 0.05)


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


async def test_chat_stream_sends_keepalive_while_the_model_is_silent(client, auth_headers, monkeypatch,
                                                                     fast_keepalive) -> None:
    from app.api.v1.endpoints import ai as ai_ep
    from app.services import system_config

    async def _cfg(_s):
        return LLMConfig(enabled=True, url="http://llm.example.test:11434", embedding_model="e", chat_model="m",
                         timeout=30.0)
    monkeypatch.setattr(system_config, "get_llm_config", _cfg)

    async def slow_stream(*a, **kw):
        yield {"type": "status", "stage": "tools"}
        await asyncio.sleep(0.4)
        yield {"type": "done", "answer": "ok", "trace_messages": [], "model": "m", "elapsed_ms": 400}
    monkeypatch.setattr(ai_ep.ai_service, "chat_stream", slow_stream)

    r = await client.post("/api/v1/ai/chat/stream", headers=auth_headers,
                          json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert r.text.count(": keepalive") >= 3, r.text[:300]
    evs = _events(r.text)
    assert [e["type"] for e in evs] == ["status", "done"]
    assert evs[-1]["conversation_id"]          # 對話紀錄照樣寫


async def test_traceroute_stream_sends_keepalive_between_silent_hops(client, auth_headers, monkeypatch,
                                                                     fast_keepalive) -> None:
    from app.services import netdiag

    async def slow(target, max_hops=30):
        yield {"type": "hop", "hop": 1, "host": "192.0.2.1"}
        await asyncio.sleep(0.4)
        yield {"type": "done", "target": target}
    monkeypatch.setattr(netdiag, "traceroute_stream", slow)

    r = await client.post("/api/v1/tools/net/traceroute/stream", headers=auth_headers,
                          json={"target": "192.0.2.1", "max_hops": 3})
    assert r.status_code == 200, r.text
    assert r.text.count(": keepalive") >= 3, r.text[:300]
    assert [e["type"] for e in _events(r.text)] == ["hop", "done"]


async def test_investigate_stream_sends_keepalive_before_the_first_word(client, auth_headers, db_session,
                                                                         monkeypatch, fast_keepalive) -> None:
    from app.services import ai as ai_mod

    from tests.test_ai_interpret_model import _address, _llm

    async def slow_stream(url, body, wait, on_chunk, headers=None, provider="ollama"):
        await asyncio.sleep(0.4)            # 模型還在讀資料，沒吐出第一個字
        await on_chunk("判讀內容", "content")
        return "判讀內容"
    monkeypatch.setattr(ai_mod, "_raw_chat_streamed", slow_stream)
    await _address(db_session)
    await _llm(db_session)

    r = await client.post("/api/v1/investigate/narrative/stream", headers=auth_headers,
                          params={"ip": "198.51.100.88"})
    assert r.status_code == 200, r.text
    assert r.text.count(": keepalive") >= 2, r.text[:300]
    assert _events(r.text)[-1]["type"] == "done"


async def test_wrapper_closes_the_upstream_when_the_client_goes_away() -> None:
    """使用者關掉畫面：背景任務要停、上游的產生器要收掉，不可以留著繼續呼叫模型。"""
    closed = asyncio.Event()

    async def model():
        try:
            yield {"type": "status"}
            await asyncio.sleep(30)
            yield {"type": "done"}
        finally:
            closed.set()

    gen = sse.with_keepalive(model(), 0.01)
    assert await gen.__anext__() == {"type": "status"}
    assert await gen.__anext__() is None      # 上游沒動靜 → 保活
    await gen.aclose()
    await asyncio.wait_for(closed.wait(), 1)


async def test_wrapper_passes_upstream_errors_through() -> None:
    async def model():
        yield {"type": "status"}
        raise RuntimeError("boom")

    gen = sse.with_keepalive(model(), 5)
    assert await gen.__anext__() == {"type": "status"}
    with pytest.raises(RuntimeError, match="boom"):
        await gen.__anext__()


async def test_wrapper_does_not_fetch_the_next_event_until_the_caller_is_done() -> None:
    """呼叫端處理 done（寫對話紀錄）時，上游不可以同時動到同一個資料庫 session。"""
    log: list[str] = []

    async def model():
        log.append("fetch-1")
        yield 1
        log.append("fetch-2")
        yield 2

    gen = sse.with_keepalive(model(), 5)
    assert await gen.__anext__() == 1
    await asyncio.sleep(0.05)                  # 呼叫端還在處理第一個
    assert log == ["fetch-1"]
    assert await gen.__anext__() == 2
    await gen.aclose()


def test_every_sse_endpoint_uses_the_keepalive() -> None:
    """守門：新的 SSE 串流端點也要送保活，否則跑超過代理逾時就同樣斷線、畫面只剩網路錯誤。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    streams = [p for p in root.rglob("*.py") if "text/event-stream" in p.read_text(encoding="utf-8")
               and p.name != "sse.py"]
    assert streams, "一個 SSE 端點都沒找到：搜尋方式失效，守門形同虛設"
    missing = [str(p.relative_to(root)) for p in streams if "sse.KEEPALIVE" not in p.read_text(encoding="utf-8")]
    assert not missing, f"這些 SSE 端點沒有送保活（請用 app.core.sse）：{missing}"
