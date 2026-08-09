"""SFTP 的純邏輯：路徑正規化、上限、目錄項目的呈現。

**與連線無關的部分都放在這裡**，這樣不必架一台 SSH 伺服器就測得到上限與路徑處理 ——
而那兩件事正是最容易出錯、也最需要被守住的。

安全性立場：路徑一律原樣交給遠端，**不在這一端做沙箱**。使用者能連 SSH 就能在 shell 裡
`cat` 任何檔案，SFTP 不會多給任何權限；真正的授權邊界在遠端主機自己。這一端要做的是
不要因為自作聰明的路徑拼接而讓人存取到「他以為不是那個」的檔案。
"""

from __future__ import annotations

import posixpath
import stat as statmod
from dataclasses import dataclass
from typing import Any

# 單一檔案的上下傳上限。IPAM 的用途是設定檔、憑證、log 片段，不是搬映像檔；
# 沒有上限的話一個 20 GB 的檔案會把瀏覽器記憶體與後端一起拖垮。
MAX_FILE_BYTES = 100 * 1024 * 1024

# 一次列出的目錄項目上限。/proc 或幾十萬個檔案的目錄不該讓畫面與連線一起卡住。
MAX_ENTRIES = 2000

# 每個 chunk 的大小；太小會讓大檔的 frame 數爆掉，太大則單一 frame 佔太多記憶體
CHUNK_BYTES = 256 * 1024


class SftpError(Exception):
    """可以直接顯示給使用者看的錯誤。"""


def normalize_path(path: str | None, *, cwd: str = "/") -> str:
    """把使用者給的路徑正規化成絕對的 POSIX 路徑。

    相對路徑以 `cwd` 為基準；`..` 交給 `normpath` 收斂。**不做沙箱**（見模組說明），
    但要確保送出去的是一條明確、沒有 `..` 殘留的路徑 —— 使用者看到的路徑就是實際
    操作的路徑，兩者不一致才是真正危險的地方。
    """
    p = (path or "").strip()
    if not p:
        p = cwd or "/"
    if not p.startswith("/"):
        p = posixpath.join(cwd or "/", p)
    out = posixpath.normpath(p)
    # normpath 會把 "/.." 收成 "/"，但保險起見再確認一次
    return out if out.startswith("/") else "/"


def check_size(size: int | None, *, what: str) -> int:
    """檔案大小檢查。`None`＝遠端沒回報大小，視為未知並拒絕（寧可不傳）。"""
    if size is None:
        raise SftpError(f"{what}：遠端沒有回報檔案大小，無法確認是否超過上限")
    if size < 0:
        raise SftpError(f"{what}：檔案大小異常")
    if size > MAX_FILE_BYTES:
        mb = MAX_FILE_BYTES // (1024 * 1024)
        raise SftpError(f"{what}：檔案超過 {mb} MB 上限（這個功能是給設定檔與紀錄用的）")
    return size


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    is_link: bool
    size: int | None
    mtime: int | None
    mode: str | None


def _mode_str(mode: int | None) -> str | None:
    if mode is None:
        return None
    try:
        return statmod.filemode(mode)
    except (TypeError, ValueError):
        return None


def to_entry(dirpath: str, name: str, attrs: Any) -> Entry:
    """把 asyncssh 的 SFTPAttrs 轉成前端要的形狀。

    屬性可能缺（有些伺服器的 readdir 不回 size/mtime）—— 缺就是 None，不要猜成 0：
    `0 bytes` 與「不知道多大」在畫面上是兩件事。
    """
    perms = getattr(attrs, "permissions", None)
    is_dir = bool(perms is not None and statmod.S_ISDIR(perms))
    is_link = bool(perms is not None and statmod.S_ISLNK(perms))
    return Entry(
        name=name,
        path=posixpath.join(dirpath, name),
        is_dir=is_dir,
        is_link=is_link,
        size=getattr(attrs, "size", None),
        mtime=getattr(attrs, "mtime", None),
        mode=_mode_str(perms),
    )


def sort_entries(entries: list[Entry]) -> list[Entry]:
    """目錄在前、再依名稱（不分大小寫）—— 與一般檔案管理員一致。"""
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))
