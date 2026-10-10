#!/usr/bin/env python3
"""每日備份的加密格式（JTBAK v1）—— 這個檔案不 import 任何 jt-ipam 的東西，可以單獨拿來用。

為什麼：每日備份裡有資料庫，也有 backend.env（SECRET_KEY／ENCRYPTION_KEY）。兩者放在一起，
拿到備份檔就等於拿到所有機密（資料庫裡的整合密碼都是用那把金鑰加密的）。設定了備份加密密碼
之後，整包備份（pg_dump、backend.env、TLS、上傳檔）壓成一個 tar.gz、加密成單一 `.jtbak` 檔，
明文的目錄刪掉。

整台主機壞掉、jt-ipam 還沒裝回去時也要解得開，所以只依賴 Python 標準函式庫與 `cryptography`
（Debian／Ubuntu：apt install python3-cryptography）：

    python3 backup_crypt.py decrypt jt-ipam-2026-10-09.jtbak --out /tmp/restore

格式：
    b"JTBAK1\\n" + 標頭 JSON（一行）+ 一串紀錄（4 bytes 長度 + 密文）
- 金鑰：scrypt(密碼, salt)，參數記在標頭
- 每個紀錄 1 MiB 明文、AES-256-GCM；nonce＝標頭的 7 bytes 前綴＋4 bytes 序號＋1 byte「最後一塊」旗標，
  附加資料是整行標頭（STREAM 結構）：調換、刪掉中間的紀錄，或從尾端截斷，都解不開
"""

from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import os
import secrets
import socket
import struct
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"JTBAK1\n"
CHUNK = 1024 * 1024
MIN_PASSPHRASE = 12
_N, _R, _P = 2**15, 8, 1


class BackupCryptError(Exception):
    """密碼錯誤、檔案損毀或被動過（解密失敗一律是這個）。"""


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _nonce(prefix: bytes, counter: int, final: bool) -> bytes:
    return prefix + struct.pack(">I", counter) + (b"\x01" if final else b"\x00")


class _Sealer(io.RawIOBase):
    """tarfile 寫進來的位元組 → 每滿 1 MiB 加密成一個紀錄寫到 out。close() 時寫最後一塊。"""

    def __init__(self, out: BinaryIO, aead: AESGCM, prefix: bytes, aad: bytes) -> None:
        self.out, self.aead, self.prefix, self.aad = out, aead, prefix, aad
        self.buf = bytearray()
        self.counter = 0
        self.done = False

    def writable(self) -> bool:
        return True

    def _emit(self, data: bytes, final: bool) -> None:
        ct = self.aead.encrypt(_nonce(self.prefix, self.counter, final), bytes(data), self.aad)
        self.out.write(struct.pack(">I", len(ct)))
        self.out.write(ct)
        self.counter += 1
        if self.counter >= 2**32:
            raise BackupCryptError("backup too large")

    def write(self, b) -> int:  # type: ignore[no-untyped-def, override]
        self.buf.extend(b)
        while len(self.buf) > CHUNK:
            self._emit(self.buf[:CHUNK], False)
            del self.buf[:CHUNK]
        return len(b)

    def close(self) -> None:
        if not self.done:
            self._emit(self.buf, True)      # 最後一塊（可能是空的）一定要寫：沒有它＝被截斷
            self.buf.clear()
            self.done = True
        super().close()


def encrypt_dir(src: Path, out_path: Path, passphrase: str) -> Path:
    """把 src 目錄整個（tar.gz）加密成 out_path。先寫 .partial、完成才改名。"""
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupCryptError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    salt = secrets.token_bytes(16)
    prefix = secrets.token_bytes(7)
    header = json.dumps({
        "format": "jt-ipam-backup", "version": 1, "kdf": "scrypt", "n": _N, "r": _R, "p": _P,
        "salt": base64.b64encode(salt).decode(), "nonce_prefix": base64.b64encode(prefix).decode(),
        "chunk": CHUNK, "created_at": datetime.now(UTC).isoformat(), "source": socket.gethostname(),
        "name": src.name,
    }, separators=(",", ":")).encode() + b"\n"
    aead = AESGCM(_key(passphrase, salt, _N, _R, _P))
    partial = out_path.with_name(out_path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(MAGIC)
        out.write(header)
        sealer = _Sealer(out, aead, prefix, header)
        with tarfile.open(fileobj=sealer, mode="w|gz") as tar:   # type: ignore[call-overload]
            tar.add(str(src), arcname=src.name)
        sealer.close()
        out.flush()
        os.fsync(out.fileno())
    os.replace(partial, out_path)
    return out_path


def _records(f: BinaryIO):  # type: ignore[no-untyped-def]
    """逐一讀出密文紀錄，並告訴呼叫端這是不是最後一個（往後多看一筆）。"""
    def read_one() -> bytes | None:
        head = f.read(4)
        if not head:
            return None
        if len(head) != 4:
            raise BackupCryptError("truncated record header")
        (n,) = struct.unpack(">I", head)
        if n > CHUNK + 64:
            raise BackupCryptError("record too large")
        body = f.read(n)
        if len(body) != n:
            raise BackupCryptError("truncated record")
        return body
    cur = read_one()
    while cur is not None:
        nxt = read_one()
        yield cur, nxt is None
        cur = nxt


def decrypt_to(path: Path, out: BinaryIO, passphrase: str) -> dict:  # type: ignore[type-arg]
    """解密成 tar.gz 寫到 out，回傳標頭。密碼錯、被截斷、被改過都丟 BackupCryptError。"""
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise BackupCryptError("not a jt-ipam encrypted backup")
        header = f.readline()
        try:
            meta = json.loads(header)
            salt = base64.b64decode(meta["salt"])
            prefix = base64.b64decode(meta["nonce_prefix"])
            aead = AESGCM(_key(passphrase, salt, int(meta["n"]), int(meta["r"]), int(meta["p"])))
        except (ValueError, KeyError, TypeError) as exc:
            raise BackupCryptError("damaged header") from exc
        counter = 0
        saw_final = False
        for ct, last in _records(f):
            try:
                out.write(aead.decrypt(_nonce(prefix, counter, last), ct, header))
            except InvalidTag as exc:
                raise BackupCryptError("wrong passphrase, or the file was truncated or modified") from exc
            counter += 1
            saw_final = last
        if not saw_final:
            raise BackupCryptError("empty or truncated backup")
    return meta


def _safe_extract(tar_path: Path, dest: Path) -> None:
    """只解一般檔案與目錄，路徑一定要在 dest 底下（不信任檔案內容）。"""
    dest = dest.resolve()
    with tarfile.open(tar_path, "r:gz") as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not (target == dest or dest in target.parents) or not (m.isfile() or m.isdir()):
                raise BackupCryptError(f"refusing to extract {m.name!r}")
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            assert src is not None
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as w:
                while chunk := src.read(CHUNK):
                    w.write(chunk)


def decrypt_file(path: Path, dest: Path, passphrase: str) -> dict:  # type: ignore[type-arg]
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest, suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            meta = decrypt_to(path, tmp, passphrase)
        except BaseException:
            tmp.close()
            tmp_path.unlink(missing_ok=True)
            raise
    try:
        _safe_extract(tmp_path, dest)
    finally:
        tmp_path.unlink(missing_ok=True)
    return meta


def _read_passphrase(args: argparse.Namespace, confirm: bool) -> str:
    if args.passphrase_fd is not None:
        with os.fdopen(args.passphrase_fd, "r", encoding="utf-8") as f:
            return f.read().rstrip("\n")
    p = getpass.getpass("Backup passphrase: ")
    if confirm and getpass.getpass("Again: ") != p:
        raise SystemExit("passphrases do not match")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="jt-ipam encrypted backup (JTBAK v1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decrypt", help="decrypt a .jtbak file into a directory")
    d.add_argument("file", type=Path)
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--passphrase-fd", type=int, default=None)
    e = sub.add_parser("encrypt", help="encrypt a directory into a .jtbak file")
    e.add_argument("dir", type=Path)
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--passphrase-fd", type=int, default=None)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "decrypt":
            meta = decrypt_file(args.file, args.out, _read_passphrase(args, False))
            print(f"decrypted {meta.get('name')} (created {meta.get('created_at')} on {meta.get('source')}) → {args.out}")
        else:
            encrypt_dir(args.dir, args.out, _read_passphrase(args, True))
            print(f"encrypted {args.dir} → {args.out}")
    except BackupCryptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
