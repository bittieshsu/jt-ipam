"""每日備份腳本：失敗要留下看得到的原因，而且不可以毀掉同一天先前成功的那份。

prod（2026-10-08 發現）：一張 postgres 擁有的殘留表讓 pg_dump 每天都「permission denied」，
備份至少失敗了六週，每天留下一個空目錄；CLI 的 doctor 看到「最新的目錄存在」就說 OK，
系統診斷頁根本沒有這一項，所以沒有人知道。

這裡用假的 pg_dump 跑真正的腳本（不碰資料庫）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "jt-ipam-backup.sh"


def _run(tmp_path: Path, pg_dump_body: str) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    pg = fake / "pg_dump"
    pg.write_text("#!/bin/bash\n" + pg_dump_body)
    pg.chmod(0o755)
    env_file = tmp_path / "backend.env"
    env_file.write_text("POSTGRES_DB=jt_ipam\nPOSTGRES_USER=jt_ipam\nPOSTGRES_PASSWORD=x\n")
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "BACKUP_DIR": str(tmp_path / "backups"),
           "ENV_FILE": str(env_file), "TLS_DIR": str(tmp_path / "no-tls"), "UPLOAD_DIR": str(tmp_path / "no-up")}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


def _status(tmp_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (tmp_path / "backups" / "last-run").read_text().splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


# 假的 pg_dump：找到 -f 後面的檔名寫入內容
_OK = 'while [[ $# -gt 0 ]]; do if [[ $1 == -f ]]; then echo dumpdata > "$2"; fi; shift; done\n'
_FAIL = ('echo "pg_dump: error: query failed: ERROR:  permission denied for table stray_backup_20260806" >&2\n'
         'while [[ $# -gt 0 ]]; do if [[ $1 == -f ]]; then echo partial > "$2"; fi; shift; done\nexit 1\n')


@pytest.mark.skipif(not SCRIPT.exists(), reason="no backup script")
def test_success_writes_a_status_file(tmp_path: Path) -> None:
    r = _run(tmp_path, _OK)
    assert r.returncode == 0, r.stderr
    st = _status(tmp_path)
    assert st["status"] == "ok"
    assert st["last_success_at"] == st["finished_at"]
    dumps = list((tmp_path / "backups").glob("*/*.dump"))
    assert len(dumps) == 1 and dumps[0].read_text().strip() == "dumpdata"


@pytest.mark.skipif(not SCRIPT.exists(), reason="no backup script")
def test_failure_records_the_reason_and_keeps_the_earlier_dump(tmp_path: Path) -> None:
    assert _run(tmp_path, _OK).returncode == 0
    first = _status(tmp_path)
    r = _run(tmp_path, _FAIL)
    assert r.returncode != 0
    st = _status(tmp_path)
    assert st["status"] == "fail"
    assert "permission denied for table stray_backup_20260806" in st["error"]
    assert st["last_success_at"] == first["last_success_at"]      # 上一次成功的時間留著
    # 同一天先前成功的那份不可以被失敗的這次蓋掉或截斷
    dumps = list((tmp_path / "backups").glob("*/*.dump"))
    assert len(dumps) == 1 and dumps[0].read_text().strip() == "dumpdata"
    assert not list((tmp_path / "backups").glob("*/*.partial"))
    # 權限問題要直接說怎麼修
    assert "OWNER TO" in r.stderr


@pytest.mark.skipif(not SCRIPT.exists(), reason="no backup script")
def test_first_ever_failure_leaves_no_empty_directory(tmp_path: Path) -> None:
    """失敗留下空目錄，會讓「最新的備份目錄」看起來像有備份。"""
    assert _run(tmp_path, _FAIL).returncode != 0
    assert not [p for p in (tmp_path / "backups").iterdir() if p.is_dir()]
    st = _status(tmp_path)
    assert st["status"] == "fail" and st.get("last_success_at", "") == ""
