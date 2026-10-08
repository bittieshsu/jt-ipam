"""排程同步遇到資料庫死結要重做一次，不要直接記成整合失敗。

prod（2026-10-08 盤點）：兩週內 OPNsense 同步 53 次失敗，全是 `DeadlockDetectedError`，
語句都是 commit 時 flush 的 `UPDATE ip_addresses SET arp_seen`。對手是同時在寫同一批 IP 的
掃描代理回報：兩邊都分好幾個階段鎖列，排序救不了。PostgreSQL 的標準做法是整筆交易重做；
不重做的代價是整合頁每天亮好幾次紅燈、那一輪的上線證據晚一個週期。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "jt-ipam-sync.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("jt_ipam_sync_script", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DeadlockDetectedError(Exception):
    """與 asyncpg 同名：判斷看例外鏈上的類別名稱與訊息。"""


class _Session:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def refresh(self, obj: Any) -> None:
        self.calls.append("refresh")


@pytest.mark.anyio
async def test_a_deadlock_is_retried_once(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    s, tries = _Session(), []

    async def work(x: int) -> int:
        tries.append(x)
        if len(tries) == 1:
            try:
                raise DeadlockDetectedError("deadlock detected")
            except DeadlockDetectedError as e:
                raise RuntimeError("(sqlalchemy) wrapped") from e
        return x * 2

    assert await mod._commit_with_retry(s, object(), "opnsense fw", work, 21) == 42
    assert tries == [21, 21]
    assert s.calls == ["rollback", "refresh", "commit"]


@pytest.mark.anyio
async def test_other_errors_and_a_second_deadlock_are_not_hidden(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    async def broken() -> None:
        raise ValueError("bad credentials")

    with pytest.raises(ValueError):
        await mod._commit_with_retry(_Session(), object(), "x", broken)

    async def always_deadlocks() -> None:
        raise DeadlockDetectedError("deadlock detected")

    s = _Session()
    with pytest.raises(DeadlockDetectedError):
        await mod._commit_with_retry(s, object(), "x", always_deadlocks)
    assert s.calls == ["rollback", "refresh"]       # 只重做一次


def test_firewall_and_dhcp_blocks_use_the_retry() -> None:
    """會寫 IP 上線證據（SightingBatch）的整合都要走重試。"""
    src = SCRIPT.read_text(encoding="utf-8")
    for call in ("fw_svc.sync_all_for_firewall", "pfsense_svc.sync_instance", "fortigate_svc.sync_instance",
                 "paloalto_svc.sync_instance", "mikrotik_svc.sync_instance", "windows_dhcp_svc.sync_instance",
                 "kea_dhcp_svc.sync_instance"):
        line = next((ln for ln in src.splitlines() if call in ln), "")
        assert line, f"找不到 {call}（改名了就更新這支測試）"
        assert "_commit_with_retry" in line, f"{call} 沒有走死結重試：{line.strip()}"


async def _no_sleep(_s: float) -> None:
    return None
