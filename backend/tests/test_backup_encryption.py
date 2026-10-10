"""每日備份加密（2026-10-09 合規核對）。

以前的每日備份是明文目錄：pg_dump 與 backend.env（SECRET_KEY／ENCRYPTION_KEY）放在一起，
拿到備份就等於拿到所有機密。設了密碼之後整包加密成 .jtbak（services/backup_crypt）。
"""
from __future__ import annotations

import ast
import io
import os
import pathlib
import struct
import tarfile

import pytest

from app.services import backup_crypt as bc

PASS = "correct horse battery staple"
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _make_backup(tmp_path: pathlib.Path, big: bool = False) -> pathlib.Path:
    d = tmp_path / "2026-10-09"
    (d / "sub").mkdir(parents=True)
    (d / "backend.env").write_text("ENCRYPTION_KEY=not-a-real-key\n")
    (d / "jt-ipam-2026-10-09.dump").write_bytes(os.urandom(2_500_000 if big else 1000))
    (d / "sub" / "uploads.tar.gz").write_bytes(b"x" * 10)
    return d


def test_roundtrip_and_plaintext_is_not_in_the_file(tmp_path) -> None:
    src = _make_backup(tmp_path)
    out = bc.encrypt_dir(src, tmp_path / "b.jtbak", PASS)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert b"not-a-real-key" not in out.read_bytes()
    dest = tmp_path / "restore"
    meta = bc.decrypt_file(out, dest, PASS)
    assert meta["name"] == "2026-10-09"
    assert (dest / "2026-10-09" / "backend.env").read_text() == "ENCRYPTION_KEY=not-a-real-key\n"
    assert (dest / "2026-10-09" / "jt-ipam-2026-10-09.dump").read_bytes() == (src / "jt-ipam-2026-10-09.dump").read_bytes()


def test_wrong_passphrase_fails(tmp_path) -> None:
    out = bc.encrypt_dir(_make_backup(tmp_path), tmp_path / "b.jtbak", PASS)
    with pytest.raises(bc.BackupCryptError):
        bc.decrypt_file(out, tmp_path / "r", "wrong passphrase!!")


def _records(raw: bytes) -> tuple[bytes, list[bytes]]:
    head_end = raw.index(b"\n", len(bc.MAGIC)) + 1
    pos, recs = head_end, []
    while pos < len(raw):
        (n,) = struct.unpack(">I", raw[pos:pos + 4])
        recs.append(raw[pos:pos + 4 + n])
        pos += 4 + n
    return raw[:head_end], recs


@pytest.mark.parametrize("attack", ["truncate_tail", "drop_last_record", "swap_records", "flip_byte"])
def test_tampering_is_detected(tmp_path, attack) -> None:
    out = bc.encrypt_dir(_make_backup(tmp_path, big=True), tmp_path / "b.jtbak", PASS)
    head, recs = _records(out.read_bytes())
    assert len(recs) >= 3
    if attack == "truncate_tail":
        data = head + b"".join(recs)[:-100]
    elif attack == "drop_last_record":
        data = head + b"".join(recs[:-1])          # 從尾端整段截掉：沒有「最後一塊」
    elif attack == "swap_records":
        data = head + recs[1] + recs[0] + b"".join(recs[2:])
    else:
        body = bytearray(b"".join(recs))
        body[50] ^= 0x01
        data = head + bytes(body)
    bad = tmp_path / "bad.jtbak"
    bad.write_bytes(data)
    with pytest.raises(bc.BackupCryptError):
        bc.decrypt_file(bad, tmp_path / "r", PASS)


def test_malicious_archive_paths_are_refused(tmp_path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../../etc/evil")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    p = tmp_path / "evil.tar.gz"
    p.write_bytes(buf.getvalue())
    with pytest.raises(bc.BackupCryptError):
        bc._safe_extract(p, tmp_path / "dest")
    assert not (tmp_path.parent / "etc" / "evil").exists()


def test_short_passphrase_is_refused(tmp_path) -> None:
    with pytest.raises(bc.BackupCryptError):
        bc.encrypt_dir(_make_backup(tmp_path), tmp_path / "b.jtbak", "short")


def test_decrypt_tool_is_standalone() -> None:
    """整台主機壞掉、jt-ipam 還沒裝回去時也要能解：不可以 import jt-ipam 的東西。"""
    tree = ast.parse(pathlib.Path(bc.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("app") for a in node.names)


def test_cli_exit_codes(tmp_path, monkeypatch) -> None:
    from app.cli import backup_encrypt

    async def none():
        return None

    async def some():
        return PASS

    src = _make_backup(tmp_path)
    monkeypatch.setattr(backup_encrypt, "_passphrase", none)
    assert backup_encrypt.main([str(src)]) == backup_encrypt.NOT_CONFIGURED
    assert src.exists(), "沒設定密碼時不能動備份"
    monkeypatch.setattr(backup_encrypt, "_passphrase", some)
    assert backup_encrypt.main([str(src)]) == 0
    assert not src.exists(), "加密完成後明文目錄要刪掉"
    assert (tmp_path / "jt-ipam-2026-10-09.jtbak").exists()


@pytest.mark.anyio
async def test_settings_endpoints(client, auth_headers, db_session) -> None:
    from sqlalchemy import select

    from app.models.audit import AuditLog
    assert (await client.put("/api/v1/system/backup-encryption", headers=auth_headers,
                             json={"passphrase": "short"})).status_code == 422
    r = await client.put("/api/v1/system/backup-encryption", headers=auth_headers, json={"passphrase": PASS})
    assert r.status_code == 200 and r.json()["enabled"] is True
    from app.services.system_config import get_backup_encryption
    assert (await get_backup_encryption(db_session))["passphrase"] == PASS
    assert (await client.delete("/api/v1/system/backup-encryption", headers=auth_headers)).status_code == 204
    assert (await client.get("/api/v1/system/backup-encryption", headers=auth_headers)).json()["enabled"] is False
    diffs = (await db_session.execute(select(AuditLog.diff).where(AuditLog.action == "update"))).scalars().all()
    changes = [d.get("change") for d in diffs if isinstance(d, dict) and d.get("setting") == "backup_encryption"]
    assert changes == ["set", "removed"]
    assert all(PASS not in str(d) for d in diffs), "稽核記錄不能變成密碼的另一份副本"


def test_backup_script_encrypts_and_prunes_encrypted_files() -> None:
    sh = (ROOT / "scripts/jt-ipam-backup.sh").read_text(encoding="utf-8")
    assert "app.cli.backup_encrypt" in sh
    assert "-name '*.jtbak'" in sh, "加密後的備份也要照保留天數清掉"
    assert 'echo "encrypted=$ENCRYPTED"' in sh


def test_doctor_warns_about_unencrypted_backups() -> None:
    from app.services.self_check import backup_encryption_check_from
    assert backup_encryption_check_from(False, None).status == "warn"
    assert backup_encryption_check_from(True, "status=ok\nencrypted=0\n").status == "warn"
    assert backup_encryption_check_from(True, "status=ok\nencrypted=1\n").status == "ok"
