"""IP 位址的 SFTP 檔案傳輸：ticket 換發 + WebSocket↔SFTP 橋接。

**獨立開關、與 SSH 同等強度的授權**（`can_use_sftp`）—— 開放傳檔與開放終端機是兩件事，
各自有自己的開關（`sftp_enabled` / `ssh_enabled`）；但授權模型刻意完全相同：能讀寫遠端
檔案的人，實質能力與能開 shell 的人同一級，不該因為「只是傳檔」而放寬。憑證同樣走個人
加密金庫或當次輸入，明文只在記憶體、用完即丟。

WS 帶不了 Authorization header → 沿用 SSH 那套：先以 JWT 換 60 秒單次 ticket，再開 WS。

協定（JSON 控制訊息 + 二進位資料框）：
  → {"type":"config", ...}                連線設定（欄位與 SSH 相同）
  ← {"type":"ready","cwd":"/root"}
  → {"type":"list","path":"/etc"}
  ← {"type":"list","path":...,"entries":[...],"truncated":bool}
  → {"type":"get","path":"/etc/hosts"}
  ← {"type":"file_begin","name":...,"size":N} → 二進位框… → {"type":"file_end"}
  → {"type":"put","path":...,"size":N} → ← {"type":"put_ready"} → 二進位框… → {"type":"ok"}
  → {"type":"mkdir"|"rename"|"delete"}    ← {"type":"ok","op":...}
  ← {"type":"error","message":...}        任何一步失敗

稽核：連線開／關，以及**每一次寫入與下載**都留紀錄。列目錄不記 —— 那是瀏覽行為，
每點一個資料夾寫一筆只會把稽核洗版，真正要看的是誰把哪個檔案拿走、放上去、刪掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import uuid
from typing import Annotated, Any

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.core.db import SessionLocal, get_session
from app.core.rate_limit import _redis_client
from app.core.security import envelope_decrypt
from app.core.tickets import take_once
from app.core.ws_timeouts import HANDSHAKE_TIMEOUT, WsTimeout, receive_text_within
from app.models.address import IPAddress
from app.models.ssh_credential import SSHCredential
from app.models.user import User
from app.services.permission import can_use_sftp
from app.services.sftp import (
    CHUNK_BYTES,
    MAX_ENTRIES,
    SftpError,
    check_size,
    describe_rmdir_failure,
    friendly_connect_error,
    friendly_error,
    normalize_path,
    sort_entries,
    to_entry,
    walk_for_delete,
)
from app.services.ssh_tunnel import LEGACY_SSH_ALGS

router = APIRouter(prefix="/addresses", tags=["sftp"])

_TICKET_TTL = 60
_CONNECT_TIMEOUT = 15.0


def _ticket_key(ticket: str) -> str:
    return f"sftp:tk:{ticket}"


@router.post("/{address_id}/sftp/ticket")
async def issue_sftp_ticket(
    address_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """換發短期一次性 ticket；之後用它開 SFTP 的 WebSocket。"""
    from app.core.rate_limit import limit_per_ip

    await limit_per_ip(request, name="ssh")

    ip = await session.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=404, detail="Address not found")
    if not await can_use_sftp(session, user=user, ip=ip):
        # 不洩漏存在性差異 —— 一律 403（與 SSH 相同）
        raise HTTPException(status_code=403, detail="無 SSH／SFTP 連線權限")

    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"user_id": str(user.id), "ip_id": str(ip.id)})
    await _redis_client().set(_ticket_key(ticket), payload, ex=_TICKET_TTL)
    return {
        "ticket": ticket,
        "ws_path": f"/api/v1/addresses/{ip.id}/sftp/ws",
        "default_port": 22,
        "ttl": _TICKET_TTL,
    }


async def _redeem(ticket: str, address_id: uuid.UUID) -> uuid.UUID | None:
    """單次取出 ticket（getdel）；回傳通過驗證的 user_id，否則 None。"""
    if not ticket:
        return None
    raw = await take_once(_redis_client(), _ticket_key(ticket))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if str(data.get("ip_id")) != str(address_id):
        return None
    try:
        return uuid.UUID(str(data.get("user_id")))
    except ValueError:
        return None


async def _connect_kwargs(
    cfg: dict[str, Any], *, user_id: uuid.UUID, address_id: uuid.UUID,
) -> tuple[str, int, dict[str, Any]]:
    """把設定訊息轉成 asyncssh 的連線參數。憑證明文只存在於這個函式回傳的 dict 裡。"""
    from app.api.v1.endpoints.ssh_credentials import cred_aad

    username = (cfg.get("username") or "").strip()
    port = int(cfg.get("port") or 22)
    if not (1 <= port <= 65535):
        raise SftpError("連接埠須為 1–65535")
    auth = cfg.get("auth")
    credential_id = cfg.get("credential_id")
    kw: dict[str, Any] = {}

    if credential_id:
        # 已存憑證：owner-only + 目標相符（與 SSH 同一條規則）
        async with SessionLocal() as s:
            try:
                cred = await s.get(SSHCredential, uuid.UUID(str(credential_id)))
            except ValueError:
                cred = None
            if (cred is None or cred.owner_user_id != user_id
                    or (cred.target_ip_id is not None
                        and str(cred.target_ip_id) != str(address_id))):
                raise SftpError("找不到可用的已存帳密")
            username = cred.username
            auth = cred.auth_type
            secrets_enc = dict(cred.secrets_enc or {})
        if auth == "password":
            kw["password"] = envelope_decrypt(
                secrets_enc["password"], aad=cred_aad(user_id, "password"))
        else:
            pk = envelope_decrypt(secrets_enc["private_key"],
                                  aad=cred_aad(user_id, "private_key"))
            pp = (envelope_decrypt(secrets_enc["passphrase"],
                                   aad=cred_aad(user_id, "passphrase"))
                  if "passphrase" in secrets_enc else None)
            kw["client_keys"] = [asyncssh.import_private_key(pk, passphrase=pp)]
            kw["preferred_auth"] = ("publickey",)
            del pk, pp
    elif auth == "password":
        kw["password"] = cfg.get("password") or ""
    elif auth == "key":
        pk = cfg.get("private_key") or ""
        pp = cfg.get("passphrase") or None
        kw["client_keys"] = [asyncssh.import_private_key(pk, passphrase=pp)]
        kw["preferred_auth"] = ("publickey",)
    else:
        raise SftpError("缺少認證方式")

    if not username:
        raise SftpError("缺少帳號")
    return username, port, kw


async def _next_text(websocket: WebSocket) -> str | None:
    """讀下一則**文字**訊息；把落單的二進位框丟掉。

    上傳中止時，客戶端可能還有幾個資料框在路上。主迴圈若直接用 `receive_text()`
    讀到它們就會壞掉，一次錯位就殺掉整條連線 —— 這裡改成跳過，讓協定自己回到同步。
    回 None 代表對方已關閉。
    """
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return None
        text = message.get("text")
        if text is not None:
            return text
        # 二進位框：這時候不該有，安靜丟掉（上一次上傳的殘餘）


#: 上傳時每一個資料框的等待上限（秒）。客戶端不送資料就不能無限期佔住這條連線。
#: 訂在這個量級是因為它要容得下慢速線路上的一個 256 KiB 框，又不能長到形同沒有保護。
UPLOAD_STALL_TIMEOUT = 30


@router.websocket("/{address_id}/sftp/ws")
async def sftp_ws(websocket: WebSocket, address_id: uuid.UUID, ticket: str = "") -> None:
    user_id = await _redeem(ticket, address_id)
    if user_id is None:
        await websocket.close(code=4401)
        return

    # 縱深重查權限：ticket 只證明「當時通過」，這裡再確認一次
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        ip = await s.get(IPAddress, address_id)
        if user is None or not user.is_active or ip is None:
            await websocket.close(code=4403)
            return
        allowed = await can_use_sftp(s, user=user, ip=ip)
        host = str(ip.ip).split("/")[0]
    if not allowed:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    actor_ip = websocket.client.host if websocket.client else None
    aid = str(address_id)

    async def send(obj: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(obj, ensure_ascii=False))

    async def audit(action: str, diff: dict[str, Any]) -> None:
        await _audit(actor_user_id=str(user_id), actor_ip=actor_ip,
                     object_id=aid, action=action, diff=diff)

    conn = None
    sftp = None
    port = 22          # 先給預設值：錯誤處理會用到，設定還沒讀到就失敗時不能是未定義
    #: 上傳途中收到的指令（客戶端放棄這次上傳、直接送下一個要求）—— 留著下一輪處理
    carry_over: str | None = None
    try:
        # 連上來卻不送設定的客戶端不可以無限期佔住這條連線（見 core/ws_timeouts）
        try:
            cfg = json.loads(await receive_text_within(
                websocket, HANDSHAKE_TIMEOUT, what="連線設定"))
        except WsTimeout as exc:
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps(
                    {"type": "error", "code": "handshake_timeout", "message": str(exc)},
                    ensure_ascii=False))
            await websocket.close(code=4408)
            return
        if cfg.get("type") != "config":
            await send({"type": "error", "message": "缺少連線設定"})
            return
        username, port, kw = await _connect_kwargs(cfg, user_id=user_id,
                                                   address_id=address_id)
        conn = await asyncssh.connect(
            host, port=port, username=username, known_hosts=None,
            connect_timeout=_CONNECT_TIMEOUT,
            **LEGACY_SSH_ALGS, **kw,
        )
        kw.clear()          # 憑證明文用完即丟
        sftp = await conn.start_sftp_client()
        try:
            cwd = await sftp.realpath(".")
        except Exception:
            cwd = "/"
        await audit("sftp_open", {"host": host, "port": port, "username": username})
        await send({"type": "ready", "cwd": str(cwd)})

        while True:
            if carry_over is not None:
                # 上一次上傳被對方的指令打斷 —— 那個指令要照常執行，不能吞掉
                msg, carry_over = carry_over, None
            else:
                msg = await _next_text(websocket)
                if msg is None:
                    break                  # 對方關閉連線
            try:
                req = json.loads(msg)
            except ValueError:
                await send({"type": "error", "message": "訊息格式錯誤"})
                continue
            op = req.get("type")
            # 失敗訊息要講得出「是哪條路徑」—— 打錯路徑時那是唯一有用的資訊
            failed_path: str | None = None
            try:
                if op == "list":
                    path = failed_path = normalize_path(req.get("path"), cwd=str(cwd))
                    names = await sftp.readdir(path)
                    entries = []
                    truncated = False
                    for a in names:
                        name = getattr(a, "filename", None)
                        if name in (".", ".."):
                            continue
                        if len(entries) >= MAX_ENTRIES:
                            truncated = True
                            break
                        entries.append(to_entry(path, str(name), a.attrs if hasattr(a, "attrs") else a))
                    await send({
                        "type": "list", "path": path, "truncated": truncated,
                        "entries": [e.__dict__ for e in sort_entries(entries)],
                    })

                elif op == "get":
                    path = failed_path = normalize_path(req.get("path"), cwd=str(cwd))
                    st = await sftp.stat(path)
                    size = check_size(getattr(st, "size", None), what="下載")
                    await send({"type": "file_begin", "path": path,
                                "name": path.rsplit("/", 1)[-1], "size": size})
                    async with sftp.open(path, "rb") as fh:
                        sent = 0
                        while sent < size:
                            data = await fh.read(min(CHUNK_BYTES, size - sent))
                            if not data:
                                break
                            await websocket.send_bytes(data)
                            sent += len(data)
                    await send({"type": "file_end", "path": path, "sent": sent})
                    await audit("sftp_download", {"path": path, "bytes": sent})

                elif op == "put":
                    path = failed_path = normalize_path(req.get("path"), cwd=str(cwd))
                    size = check_size(req.get("size"), what="上傳")
                    # ⚠️ 先把檔案開起來，成功了才叫對方送資料。
                    # 反過來（先說 put_ready 再開檔）在開檔失敗時會壞掉：客戶端已經
                    # 開始送二進位框，而伺服器跳去回報錯誤、回到主迴圈讀「文字訊息」，
                    # 讀到的卻是那些二進位框 → 協定錯位 → 整條連線死掉，連帶把後面
                    # 還沒上傳的檔案一起拖走。實機上就是這樣：第一個檔案因為遠端
                    # 回「找不到檔案或目錄」而失敗，第二個檔案顯示「連線已中斷」。
                    fh = await sftp.open(path, "wb")
                    # 開檔成功了才通知對方可以送 —— 這一行在 0.5.225 改寫時被弄丟，
                    # 造成客戶端永遠等不到「可以送了」，上傳完全失效。
                    await send({"type": "put_ready", "path": path})
                    written = 0
                    stalled = False
                    async with fh:
                        while written < size:
                            try:
                                # ⚠️ 兩件事都要防：
                                # (1) 逾時 —— 客戶端在 put_ready 之後沒把資料送完，
                                #     沒有時限就會無限期佔住這條連線。
                                # (2) **框的型別不如預期** —— 客戶端在資料還沒送完就
                                #     改送下一個指令（放棄這次上傳卻沒告知）。這時候
                                #     `receive_bytes()` 會丟 `KeyError: 'bytes'`，
                                #     整個 handler 當掉、連線關閉。實機日誌抓到的就是
                                #     這一行：使用者看到「連線已中斷」，而原因只是
                                #     伺服器對一個文字框沒有防備。
                                message = await asyncio.wait_for(
                                    websocket.receive(), timeout=UPLOAD_STALL_TIMEOUT)
                            except TimeoutError:
                                stalled = True
                                break
                            if message.get("type") == "websocket.disconnect":
                                raise WebSocketDisconnect(message.get("code", 1005))
                            chunk = message.get("bytes")
                            if chunk is None:
                                # 對方改送指令了 → 這次上傳視同放棄，把那個指令留著
                                # 等一下照常處理，不要把它丟掉也不要因此斷線。
                                carry_over = message.get("text")
                                stalled = True
                                break
                            # 用戶端多送的部分一律截斷 —— 宣告多少就寫多少，
                            # 否則上限可以被「宣告小、實際送大」繞過
                            take = chunk[: size - written]
                            await fh.write(take)
                            written += len(take)
                    if stalled:
                        # 只讓這次上傳失敗，連線繼續可用 —— 一次上傳中斷不該逼人重連。
                        # 檔案已經被建出來（可能是 0 位元組），把它清掉再回報，
                        # 免得遠端留下一個看起來成功、其實是空的檔案。
                        with contextlib.suppress(Exception):
                            await sftp.remove(path)
                        await send({"type": "error", "op": "put", "path": path,
                                    "code": "put_stalled",
                                    "message": f"上傳中斷（已寫入 {written}/{size} 位元組，"
                                               f"檔案已移除）"})
                        failed_path = None
                        continue
                    await send({"type": "ok", "op": "put", "path": path, "bytes": written})
                    await audit("sftp_upload", {"path": path, "bytes": written})

                elif op == "mkdir":
                    path = failed_path = normalize_path(req.get("path"), cwd=str(cwd))
                    await sftp.mkdir(path)
                    await send({"type": "ok", "op": "mkdir", "path": path})
                    await audit("sftp_mkdir", {"path": path})

                elif op == "rename":
                    src = normalize_path(req.get("path"), cwd=str(cwd))
                    dst = normalize_path(req.get("to"), cwd=str(cwd))
                    await sftp.rename(src, dst)
                    await send({"type": "ok", "op": "rename", "path": src, "to": dst})
                    await audit("sftp_rename", {"from": src, "to": dst})

                elif op == "delete":
                    path = failed_path = normalize_path(req.get("path"), cwd=str(cwd))
                    if not req.get("is_dir"):
                        await sftp.remove(path)
                        await send({"type": "ok", "op": "delete", "path": path})
                        await audit("sftp_delete", {"path": path, "is_dir": False})
                    elif req.get("recursive"):
                        # 連同內容刪除：由前端明示（它會先確認過項目數）。
                        # 深度優先、且不跟著符號連結走 —— 見 services/sftp.walk_for_delete。
                        plan = await walk_for_delete(sftp, path)
                        for kind, target in plan:
                            if kind == "dir":
                                await sftp.rmdir(target)
                            else:
                                await sftp.remove(target)
                        await send({"type": "ok", "op": "delete", "path": path,
                                    "removed": len(plan)})
                        await audit("sftp_delete", {"path": path, "is_dir": True,
                                                    "recursive": True,
                                                    "removed": len(plan)})
                    else:
                        try:
                            await sftp.rmdir(path)
                        except Exception as exc:      # 失敗原因要自己查，見下方註解
                            # SFTP v3 對「目錄非空」只會回一個沒有意義的通用失敗
                            # （asyncssh: SFTPFailure("Failure")），所以這裡自己列一次
                            # 目錄，才講得出「還有幾個項目」以及下一步能做什麼。
                            message, was_empty = await describe_rmdir_failure(
                                sftp, path, exc)
                            await send({"type": "error", "op": "delete", "path": path,
                                        "code": "dir_empty_required" if was_empty
                                                else "dir_not_empty",
                                        "message": message})
                            failed_path = None
                            continue
                        await send({"type": "ok", "op": "delete", "path": path})
                        await audit("sftp_delete", {"path": path, "is_dir": True})

                elif op == "put_abort":
                    # 客戶端明講「這次上傳我放棄了」。真正的清理在上傳迴圈裡已經做完
                    # （那邊會發現框型別不對而結束），這裡只要不把它當成未知指令即可。
                    continue

                elif op == "close":
                    break
                else:
                    await send({"type": "error", "message": f"不支援的操作：{op}"})
            except SftpError as exc:
                await send({"type": "error", "message": str(exc)})
            except (asyncssh.SFTPError, OSError) as exc:
                # 遠端拒絕（權限不足、檔案不存在…）是正常情況，要把原因說成人話：
                # 原本直接回 "SFTPNoSuchFile: No such file"，看不出是哪條路徑
                await send({"type": "error",
                            "message": friendly_error(exc, path=failed_path)})

    except WebSocketDisconnect:
        pass
    except SftpError as exc:
        with_suppress = getattr(websocket, "client_state", None)
        if with_suppress is not None:
            try:
                await send({"type": "error", "message": str(exc)})
            except Exception:
                pass
    except (asyncssh.Error, OSError) as exc:
        try:
            await send({"type": "error",
                        "message": friendly_connect_error(exc, host=host, port=port)})
        except Exception:
            pass
    finally:
        if sftp is not None:
            sftp.exit()
        if conn is not None:
            conn.close()
        await _audit(actor_user_id=str(user_id), actor_ip=actor_ip, object_id=aid,
                     action="sftp_close", diff={"host": host})
        try:
            await websocket.close()
        except Exception:
            pass


async def _audit(
    *, actor_user_id: str, actor_ip: str | None, object_id: str,
    action: str, diff: dict[str, Any],
) -> None:
    """以獨立短交易寫一筆稽核（不含任何憑證內容）。"""
    from app.core.audit import append_audit

    async with SessionLocal() as s:
        await append_audit(
            s, actor_user_id=actor_user_id, actor_ip=actor_ip, actor_user_agent=None,
            object_type="ip", object_id=object_id, action=action, diff=diff,
            request_id=None,
        )
        await s.commit()
