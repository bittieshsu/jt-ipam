"""MCP 金鑰的最後使用時間（使用者 2026-10-21）。

起因：使用者 10-15 在網頁上重產了 MCP 金鑰，自己的 Claude 設定還是舊金鑰，一週後才發現 MCP 不能用。
設定頁看得到「最後使用時間／來源 IP」，重產前就知道還有用戶端在用；重產後歸零（記錄綁金鑰指紋）。
同一把金鑰 60 秒內只寫一次，MCP 的每個請求不會都寫資料庫。
"""
from __future__ import annotations

import pytest

from app.services import system_config as sc

pytestmark = pytest.mark.anyio


async def _enable(client, auth_headers) -> str:
    r = await client.patch("/api/v1/system/llm", headers=auth_headers, json={"mcp_external_enabled": True})
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/system/llm/mcp-key/rotate", headers=auth_headers, json={"expires_in_days": 30})
    return r.json()["api_key"]


async def test_last_used_is_shown_and_resets_on_rotation(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(sc, "_mcp_use_written", {})
    key = await _enable(client, auth_headers)
    before = (await client.get("/api/v1/system/llm", headers=auth_headers)).json()
    assert before["mcp_api_key_last_used_at"] is None

    r = await client.post("/api/mcp/", headers={"X-Auth-Token": key},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200, r.text
    after = (await client.get("/api/v1/system/llm", headers=auth_headers)).json()
    assert after["mcp_api_key_last_used_at"] is not None
    assert after["mcp_api_key_last_used_ip"]

    # 重產 → 新金鑰從沒用過
    await client.post("/api/v1/system/llm/mcp-key/rotate", headers=auth_headers, json={"expires_in_days": 30})
    rotated = (await client.get("/api/v1/system/llm", headers=auth_headers)).json()
    assert rotated["mcp_api_key_last_used_at"] is None


async def test_writes_are_throttled(db_session, monkeypatch) -> None:
    monkeypatch.setattr(sc, "_mcp_use_written", {})
    calls = []
    orig = sc._save_mcp_use

    async def spy(*a, **k):
        calls.append(1)
        return await orig(*a, **k)
    monkeypatch.setattr(sc, "_save_mcp_use", spy)
    for _ in range(5):
        await sc.record_mcp_key_use(db_session, "jtmcp_example", "192.0.2.9")
    assert len(calls) == 1
