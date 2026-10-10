"""每日備份加密（由 root 的 /usr/local/bin/jt-ipam-backup.sh 呼叫）。

    python -m app.cli.backup_encrypt /var/backups/jt-ipam/2026-10-09
        → /var/backups/jt-ipam/jt-ipam-2026-10-09.jtbak，明文目錄刪除

結束碼：0＝已加密（印出檔名）；3＝沒有設定備份加密密碼（備份維持明文目錄）；其他＝失敗
（明文目錄保留、備份腳本把這一輪記成失敗，系統診斷看得到）。
解密不需要這支（也不需要資料庫）：python3 app/services/backup_crypt.py decrypt <檔案> --out <目錄>
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

NOT_CONFIGURED = 3


async def _passphrase() -> str | None:
    from app.core.db import SessionLocal, engine
    from app.services.system_config import get_backup_encryption
    try:
        async with SessionLocal() as session:
            return (await get_backup_encryption(session))["passphrase"]
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir", type=Path, help="the dated backup directory to encrypt")
    args = ap.parse_args(argv)
    src: Path = args.dir.resolve()
    if not src.is_dir():
        print(f"error\tnot a directory: {src}", file=sys.stderr)
        return 1
    passphrase = asyncio.run(_passphrase())
    if not passphrase:
        print("not-configured")
        return NOT_CONFIGURED
    from app.services.backup_crypt import BackupCryptError, encrypt_dir
    out = src.parent / f"jt-ipam-{src.name}.jtbak"
    try:
        encrypt_dir(src, out, passphrase)
    except (BackupCryptError, OSError) as exc:
        print(f"error\t{exc}", file=sys.stderr)
        return 1
    shutil.rmtree(src)
    print(f"encrypted\t{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
