#!/usr/bin/env python3
"""OUI 廠商庫排程更新：由 jt-ipam-oui-refresh.timer 每月觸發（安裝時也跑一次，讓新站台一開始就有資料）。

以前正式機上的 unit 指向這支，但這支從來沒進過版控 —— 每個月都 exit 2，而客戶站台連 unit 都沒有。
與 scripts/geoip_refresh.py 同套路：自帶 async session，讀 backend.env。
"""
from __future__ import annotations

import asyncio
import sys


async def _main() -> int:
    from app.core.db import SessionLocal, engine
    from app.services.oui import refresh_oui_db

    try:
        async with SessionLocal() as session:
            result = await refresh_oui_db(session)
        print(f"[oui_refresh] {result}")
        return 0
    except Exception as exc:  # 下載失敗：記錄原因、回非零讓 systemd 標記失敗；既有資料不受影響
        print(f"[oui_refresh] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
