"""AI 巡檢：一開始就記下這次嘗試，而且不再跑在 10 分鐘上限的同步服務裡（2026-10-12）。

起因：巡檢跑在 jt-ipam-sync.service 裡（TimeoutStartSec=600），超過 10 分鐘就被 systemd 砍掉；
「上次執行時間」要跑完才寫，於是 5 分鐘後下一輪又判定「還沒跑」整個重來 —— 10-04 與 10-10
都跑了兩次，10-10 那次 LLM 主機在第一輪就掛了，jt-ipam 還一直等到被砍。
- 開始送 LLM 之前就記下嘗試時間：被砍、當機、主機掛掉都算這個時段跑過，等下一個排程
- 正在跑的標記跨行程有效（手動與排程不重疊）；行程被砍留下的舊標記過了上限就不算
- 排程巡檢改由獨立的 jt-ipam-ai-audit.timer 執行，逾時上限放得下最多批次 × 每批逾時
"""
from __future__ import annotations

import ast
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.services import ai_audit as aa
from app.services.system_config import (
    get_ai_audit_last_run,
    get_ai_audit_running_since,
    mark_ai_audit_started,
)

ROOT = Path(__file__).resolve().parents[2]


class _FakeUser:
    id = uuid.uuid4()


async def _coro(v):  # type: ignore[no-untyped-def]
    return v


def _snapshot(n_ips: int) -> dict:
    return {"subnets": [{"id": "x", "cidr": "192.0.2.0/24"}], "devices": [], "empty": False,
            "ips": [{"ip": f"192.0.2.{i}", "hostname": f"h{i}"} for i in range(1, n_ips + 1)]}


async def test_attempt_is_recorded_before_the_model_is_called(monkeypatch, db_session) -> None:
    """行程在第 2 批被砍（這裡用例外模擬）：上次執行時間已經記下，排程不會 5 分鐘後重跑。"""
    from app.services import ai as ai_mod
    calls = {"n": 0}

    async def fake_chat(s, p, **kw):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("killed by systemd")
        return '{"findings":[]}'

    monkeypatch.setattr(aa, "_collect", lambda s, u: _coro(_snapshot(130)))   # 3 批
    monkeypatch.setattr(ai_mod, "raw_chat", fake_chat)
    before = datetime.now(UTC)
    with pytest.raises(RuntimeError):
        await aa.run_audit(db_session, _FakeUser())
    last = await get_ai_audit_last_run(db_session)
    assert last is not None
    assert last >= before - timedelta(seconds=1)
    assert not aa.due(last, ["03:30"], now=last + timedelta(minutes=5)), "被砍之後 5 分鐘不可以又判定該跑"


async def test_running_marker_blocks_another_process(monkeypatch, db_session) -> None:
    """另一個行程（例如手動執行的後端）正在跑：這裡不再開一次，也不打 LLM。"""
    from app.services import ai as ai_mod
    called = {"n": 0}

    async def fake_chat(s, p, **kw):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return '{"findings":[]}'

    monkeypatch.setattr(aa, "_collect", lambda s, u: _coro(_snapshot(5)))
    monkeypatch.setattr(ai_mod, "raw_chat", fake_chat)
    await mark_ai_audit_started(db_session, at=datetime.now(UTC) - timedelta(minutes=5))
    with pytest.raises(aa.AuditBusy):
        await aa.run_audit(db_session, _FakeUser())
    assert called["n"] == 0


async def test_stale_marker_from_a_killed_process_does_not_block(monkeypatch, db_session) -> None:
    from app.services import ai as ai_mod
    monkeypatch.setattr(aa, "_collect", lambda s, u: _coro(_snapshot(5)))
    monkeypatch.setattr(ai_mod, "raw_chat", lambda s, p, **kw: _coro('{"findings":[]}'))
    await mark_ai_audit_started(db_session, at=datetime.now(UTC) - aa.RUN_STALE_AFTER - timedelta(minutes=1))
    r = await aa.run_audit(db_session, _FakeUser())
    assert r.error is None
    assert await get_ai_audit_running_since(db_session) is None, "跑完要清掉正在跑的標記"


async def test_marker_is_cleared_when_every_batch_fails(monkeypatch, db_session) -> None:
    from app.services import ai as ai_mod

    async def down(s, p, **kw):  # type: ignore[no-untyped-def]
        raise ai_mod.AIUnreachable("transport: ConnectError: All connection attempts failed")

    monkeypatch.setattr(aa, "_collect", lambda s, u: _coro(_snapshot(5)))
    monkeypatch.setattr(ai_mod, "raw_chat", down)
    r = await aa.run_audit(db_session, _FakeUser())
    assert r.error
    assert await get_ai_audit_running_since(db_session) is None


def test_stale_limit_covers_the_longest_possible_run() -> None:
    """舊標記的上限要比一次巡檢最長可能跑多久還長，否則正在跑的會被當成遺留的。"""
    worst = math.ceil(aa.MAX_SAMPLE / aa.MAX_IPS_PER_BATCH) * aa.AUDIT_TIMEOUT
    assert aa.RUN_STALE_AFTER.total_seconds() > worst


# ─────────────────── 排程：獨立的 systemd 服務 ───────────────────

SYNC = ROOT / "scripts" / "jt-ipam-sync.py"


def _func(name: str) -> ast.AST:
    tree = ast.parse(SYNC.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def test_regular_sync_no_longer_runs_the_audit() -> None:
    src = ast.unparse(_func("_run"))
    assert "run_audit" not in src, "巡檢還在每 5 分鐘、10 分鐘上限的同步服務裡"


def test_ai_audit_mode_runs_the_audit() -> None:
    src = SYNC.read_text(encoding="utf-8")
    assert "--ai-audit" in src
    body = ast.unparse(_func("_run_ai_audit"))
    assert "run_audit" in body and "due(" in body and "get_ai_audit_last_run" in body


def test_ai_audit_unit_timeout_fits_the_longest_run() -> None:
    svc = (ROOT / "deploy" / "systemd" / "jt-ipam-ai-audit.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "systemd" / "jt-ipam-ai-audit.timer").read_text(encoding="utf-8")
    assert "jt-ipam-sync.py --ai-audit" in svc
    m = re.search(r"^TimeoutStartSec=(\d+)", svc, re.M)
    assert m is not None
    worst = math.ceil(aa.MAX_SAMPLE / aa.MAX_IPS_PER_BATCH) * aa.AUDIT_TIMEOUT
    assert int(m.group(1)) > worst, "逾時上限放不下最多批次 × 每批逾時，又會被 systemd 砍掉"
    assert "Unit=jt-ipam-ai-audit.service" in timer


def test_installer_handles_the_ai_audit_timer_everywhere() -> None:
    """安裝、升級都要裝（升級不重裝其他主單元檔，所以要有自己的步驟）；移除要拿掉；健檢要看。"""
    sh = (ROOT / "scripts" / "jt-ipam.sh").read_text(encoding="utf-8")

    def body(name: str) -> str:
        start = sh.index(f"\n{name}() {{")
        return sh[start:sh.index("\n}\n", start)]

    assert "install_ai_audit_timer" in body("cmd_install")
    assert "install_ai_audit_timer" in body("cmd_upgrade")
    assert "jt-ipam-ai-audit.timer" in body("cmd_uninstall")
    assert "jt-ipam-ai-audit.service" in body("cmd_uninstall")
    assert "jt-ipam-ai-audit.timer" in body("cmd_doctor")
