"""系統診斷的「每日備份」：備份壞了要看得出來，也會因此發系統告警（status=bad）。

prod（2026-10-08）：pg_dump 被一張殘留表擋住，備份至少失敗六週；診斷頁沒有這一項、
CLI doctor 看到空目錄也說 OK。判斷邏輯是純函式，這裡不碰 systemd 與檔案系統。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.self_check import backup_check_from

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
TIMER_ON = {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active"}


def _status(**kw: str) -> str:
    base = {"status": "ok", "finished_at": "2026-10-08T03:31:00+08:00", "exit_code": "0",
            "last_success_at": "2026-10-08T03:31:00+08:00", "dump_size": "5.4M", "error": ""}
    base.update(kw)
    return "\n".join(f"{k}={v}" for k, v in base.items()) + "\n"


def test_recent_success_is_ok() -> None:
    c = backup_check_from(_status(), {"Result": "success"}, TIMER_ON, now=NOW)
    assert c is not None and c.status == "ok"
    assert c.params["size"] == "5.4M"


def test_failure_is_bad_with_the_reason_and_the_fix() -> None:
    st = _status(status="fail", exit_code="1", last_success_at="",
                 error="pg_dump: error: query failed: ERROR:  permission denied for table stray_20260806")
    c = backup_check_from(st, {"Result": "exit-code"}, TIMER_ON, now=NOW)
    assert c is not None and c.status == "bad"
    assert "permission denied for table stray_20260806" in c.params["error"]
    assert "ALTER TABLE public.stray_20260806 OWNER TO" in c.params["cmd"]


def test_last_success_two_days_ago_is_bad_even_if_the_last_run_said_ok() -> None:
    old = (NOW - timedelta(hours=60)).isoformat()
    c = backup_check_from(_status(finished_at=old, last_success_at=old), {"Result": "success"}, TIMER_ON, now=NOW)
    assert c is not None and c.status == "bad"
    assert c.params["hours"] == "60"


def test_without_a_status_file_the_unit_result_decides() -> None:
    """升級前的舊腳本不寫狀態檔：退回看 systemd 的結果。"""
    bad = backup_check_from(None, {"Result": "exit-code", "ExecMainExitTimestamp": "Thu 2026-10-08 03:31:37 CST"},
                            TIMER_ON, now=NOW)
    assert bad is not None and bad.status == "bad"
    never = backup_check_from(None, {"Result": "success", "ExecMainExitTimestamp": ""}, TIMER_ON, now=NOW)
    assert never is not None and never.status == "warn"


def test_timer_disabled_is_a_warning_and_missing_systemd_is_skipped() -> None:
    off = backup_check_from(_status(), {"Result": "success"},
                            {"LoadState": "loaded", "UnitFileState": "disabled", "ActiveState": "inactive"}, now=NOW)
    assert off is not None and off.status == "warn" and "enable" in off.fix
    # 開發環境、或不是用安裝腳本裝的：沒有這個 unit 就不檢查（不要每台開發機都亮警告）
    assert backup_check_from(None, {}, {"LoadState": "not-found"}, now=NOW) is None
