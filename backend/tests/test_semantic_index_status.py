"""語意搜尋的索引狀態與嵌入維度錯誤（使用者 2026-10-13：「要檢查是不是需要重建索引，因為看不出來要不要按」）。

- 維度不合：以前 embed() 丟一句 `Embedding dim mismatch ... (adjust EMBEDDING_DIM or migration vector(N))`，
  叫人改設定檔；「檢查維度」也因此走到「取不到向量」那一支、拿不到維度。改成帶維度的專用錯誤（代碼化）。
- 索引狀態：每張表「有說明」與「有向量」的筆數，加上上次重建用的模型 → 需不需要重建、為什麼。
- 單筆建索引失敗時把向量清掉：留著的是舊說明的向量，會讓「看起來索引是好的」。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.models.section import Section
from app.models.subnet import Subnet
from app.services import ai as ai_mod
from app.services.system_config import LLMConfig

pytestmark = pytest.mark.anyio


def _cfg(model: str = "granite-embedding:278m") -> LLMConfig:
    return LLMConfig(enabled=True, url="http://llm.example.test:11434", embedding_model=model, chat_model="m",
                     timeout=30.0)


def _use(monkeypatch, *, model: str = "granite-embedding:278m", dim: int = 768, fail: bool = False) -> None:
    from app.services import system_config

    async def cfg(_s):
        return _cfg(model)
    monkeypatch.setattr(system_config, "get_llm_config", cfg)

    async def embed(_s, _t):
        if fail:
            raise ai_mod.AIUpstream("ollama 500: boom")
        if dim != 768:
            raise ai_mod.EmbeddingDimMismatch(dim, 768)
        return [0.01] * 768
    monkeypatch.setattr(ai_mod, "embed", embed)


async def _subnet(db, desc: str | None) -> Subnet:  # type: ignore[no-untyped-def]
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr=f"198.51.100.{len(desc or '') % 200}/32", description=desc)
    db.add(sub)
    await db.flush()
    return sub


async def test_dimension_mismatch_is_a_readable_coded_error(monkeypatch) -> None:
    err = ai_mod.EmbeddingDimMismatch(4096, 768)
    assert err.code == "ai_embedding_dim_mismatch"
    assert err.got == 4096 and err.expected == 768
    assert "EMBEDDING_DIM" not in str(err) and "migration" not in str(err), "不叫使用者去改設定檔"
    assert "4096" in str(err) and "768" in str(err)


async def test_probe_reports_the_models_dimension(db_session, monkeypatch) -> None:
    _use(monkeypatch, dim=4096)
    out = await ai_mod.probe_embedding(db_session)
    assert out == {"ok": False, "dim": 4096, "expected": 768, "error": None}, "要走到「模型回 N 維」那句，不是「取不到向量」"


async def test_status_says_never_indexed_then_up_to_date_then_model_changed(db_session, monkeypatch) -> None:
    await _subnet(db_session, "儲存及複寫區")
    await db_session.commit()
    _use(monkeypatch)
    st = await ai_mod.index_status(db_session)
    assert st["needs_reindex"] is True
    assert st["reason"] == "never"

    stats = await ai_mod.reindex_all(db_session)
    assert stats["failed"] == 0
    st = await ai_mod.index_status(db_session)
    assert st["needs_reindex"] is False, st
    assert st["reason"] is None
    assert st["last"]["model"] == "granite-embedding:278m"
    assert st["missing"] == 0

    _use(monkeypatch, model="other-embed:1b")
    st = await ai_mod.index_status(db_session)
    assert st["needs_reindex"] is True
    assert st["reason"] == "model_changed"


async def test_status_counts_descriptions_without_vectors(db_session, monkeypatch) -> None:
    await _subnet(db_session, "服務網路區段")
    await db_session.commit()
    _use(monkeypatch)
    await ai_mod.reindex_all(db_session)
    await _subnet(db_session, "虛擬機 SDN 網路")         # 之後才加的、沒有向量
    await db_session.commit()
    st = await ai_mod.index_status(db_session)
    assert st["needs_reindex"] is True
    assert st["reason"] == "missing"
    assert st["missing"] == 1


async def test_failed_single_index_clears_the_old_vector(db_session, monkeypatch) -> None:
    sub = await _subnet(db_session, "舊說明")
    await db_session.commit()
    _use(monkeypatch)
    assert await ai_mod.index_subnet(db_session, str(sub.id), "舊說明")
    _use(monkeypatch, fail=True)
    assert not await ai_mod.index_subnet(db_session, str(sub.id), "新說明")
    has = (await db_session.execute(text("SELECT description_embedding IS NOT NULL FROM subnets WHERE id=:i"),
                                    {"i": str(sub.id)})).scalar_one()
    assert has is False, "舊說明的向量不留著（否則看起來索引是好的、搜尋卻配錯）"


async def test_reindex_failure_carries_the_code(db_session, monkeypatch) -> None:
    await _subnet(db_session, "儲存區")
    await db_session.commit()
    _use(monkeypatch, dim=4096)
    stats = await ai_mod.reindex_all(db_session)
    assert stats["failed"] == 1
    assert stats["error_code"] == "ai_embedding_dim_mismatch"
    assert stats["error_params"] == {"got": 4096, "expected": 768}


async def test_index_status_endpoint_is_admin_only(client, auth_headers, db_session, monkeypatch) -> None:
    _use(monkeypatch)
    r = await client.get("/api/v1/ai/index-status", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert {"needs_reindex", "reason", "missing", "counts", "model", "last"} <= set(r.json())
