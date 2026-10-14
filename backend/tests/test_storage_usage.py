"""系統診斷的「容量用量」（使用者 2026-10-14：「系統診斷裡面也要顯示 jt-ipam 各個功能吃掉多少容量、資料庫吃多少」）。

- 資料庫：總大小、每張表（含索引與 TOAST）、依功能分類加總
- 主機上的目錄：備份、程式與套件、資料目錄、日誌（量不完就標「約」，讀不到就說讀不到）
- 守門：每張資料表都要歸到一個功能分類 —— 新增資料表沒分類時這裡會擋，不會默默落到「其他」
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.services import storage_usage as su

pytestmark = pytest.mark.anyio


async def test_every_table_belongs_to_a_feature_group(db_session) -> None:
    names = (await db_session.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname = 'public'"))).scalars().all()
    assert len(names) > 100
    other = sorted(n for n in names if su.group_of(n) == "other")
    assert other == [], f"這些資料表沒有歸到功能分類（app/services/storage_usage.py 的 GROUP_RULES）：{other}"


async def test_database_usage_adds_up(db_session) -> None:
    out = await su.database_usage(db_session)
    assert out["bytes"] > 0
    assert out["tables"], "至少列出幾張表"
    sizes = [t["bytes"] for t in out["tables"]]
    assert sizes == sorted(sizes, reverse=True), "由大到小"
    keys = [g["key"] for g in out["groups"]]
    assert "ipam" in keys
    assert len(keys) == len(set(keys))
    assert all(g["bytes"] >= 0 and g["tables"] >= 1 for g in out["groups"])
    assert sum(g["bytes"] for g in out["groups"]) <= out["bytes"], "表的加總不會超過整個資料庫"


def test_directory_size_and_children(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f1").write_bytes(b"x" * 10_000)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "f2").write_bytes(b"y" * 20_000)
    os.symlink("/", tmp_path / "loop")           # 符號連結不跟（不然會走遍整台機器）
    d = su.measure_dir("install", tmp_path, children=[("venv", ["a"]), ("dist", ["b"]), ("gone", ["nope"])])
    assert d["exists"] is True
    assert d["bytes"] >= 30_000
    kids = {c["key"]: c for c in d["children"]}
    assert 10_000 <= kids["venv"]["bytes"] < d["bytes"]
    assert kids["dist"]["bytes"] >= 20_000
    assert kids["gone"]["exists"] is False


def test_missing_dir_and_time_budget(tmp_path: Path) -> None:
    gone = su.measure_dir("logs", tmp_path / f"nope-{uuid.uuid4().hex}")
    assert gone["exists"] is False
    assert gone["bytes"] is None
    for i in range(50):
        (tmp_path / f"f{i}").write_bytes(b"z")
    partial = su.measure_dir("data", tmp_path, budget=0.0)
    assert partial["approx"] is True, "時間到了就停，標成「約」"


async def test_endpoint_is_admin_only(client, auth_headers, db_session) -> None:
    from app.models.user import User
    from app.services.auth import issue_access_token
    r = await client.get("/api/v1/system/doctor/storage", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["database"]["bytes"] > 0
    assert {"groups", "tables"} <= set(body["database"])
    assert isinstance(body["directories"], list)
    assert body["disk"] is None or body["disk"]["total"] > 0
    u = User(username=f"v-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@example.invalid",
             password_hash="x", is_active=True, is_admin=False)
    db_session.add(u)
    await db_session.commit()
    r = await client.get("/api/v1/system/doctor/storage", headers={"Authorization": f"Bearer {issue_access_token(u)}"})
    assert r.status_code == 403


def test_backups_fall_back_to_the_backup_scripts_record(tmp_path: Path, monkeypatch) -> None:
    """每日備份的子目錄是 root 建的（0700），後端讀不到 —— 正式站台量到 1.7 MB、實際 40 MB。
    備份腳本（root 執行）把整個目錄的大小寫進 last-run，讀不到時改用它並標出記錄時間。"""
    (tmp_path / "last-run").write_text("status=ok\nfinished_at=2026-10-14T03:31:13+08:00\ntotal_bytes=41943040\n")
    monkeypatch.setattr(su, "_walk", lambda _p, _d: (1_700_000, False, True))     # 有讀不到的子目錄
    d = su.measure_backups(tmp_path)
    assert d["bytes"] == 41943040
    assert d["from_status"] == "2026-10-14T03:31:13+08:00"
    assert d["denied"] is False

    monkeypatch.setattr(su, "_walk", lambda _p, _d: (5000, False, False))         # 讀得到就照實量
    d = su.measure_backups(tmp_path)
    assert d["bytes"] == 5000
    assert d.get("from_status") is None

    (tmp_path / "last-run").write_text("status=ok\n")                              # 舊腳本沒有 total_bytes
    monkeypatch.setattr(su, "_walk", lambda _p, _d: (1_700_000, False, True))
    d = su.measure_backups(tmp_path)
    assert d["bytes"] == 1_700_000
    assert d["denied"] is True, "沒有記錄可用就照實說讀不到"


def test_backup_script_records_the_total_size() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts" / "jt-ipam-backup.sh").read_text()
    assert 'echo "total_bytes=' in script, "last-run 要寫整個備份目錄的大小（後端讀不到 root 的子目錄）"
