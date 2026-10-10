"""開著的主控台也要跟著權限走（2026-10-09 合規核對）。

主控台（SSH／SFTP／RDP／VNC／noVNC／BMC／RustDesk 網頁客戶端）只在 WebSocket 打開的那一刻檢查
帳號與權限。之後帳號被停用、被強制登出、自己登出，或是被收回這個 IP 的連線權限，已經開著的
連線照樣用到關掉為止（SSH／RDP／VNC 刻意沒有閒置逾時，可以開一整天）。

這裡每 30 秒重新檢查一次：
- 帳號還在、還是啟用的
- 開這個主控台的那個登入工作階段還在（登出或被撤銷 → 結束）
- 對這個 IP 仍然有這種主控台的權限

不通過就先以 4403 關閉 WebSocket（reason 帶原因，前端據此說明），再取消連線（各主控台既有的收尾
照常執行，關閉的稽核照常寫），另寫一筆 `console_revoked` 稽核說明原因。順序不能反：收尾自己的
`websocket.close()` 是 1000，先送出去的話瀏覽器就看不出是權限被收回。

用法：ticket 換發時 payload 加上 `**ticket_fields(request)`；兌換 ticket 時呼叫 `note_ticket(raw)`；
WebSocket 處理函式掛 `@watch_console("ssh")`。守門：tests/test_console_guard.py。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

log = logging.getLogger("jt_ipam.console_guard")

#: 重新檢查的間隔（秒）；測試會調小
WATCH_INTERVAL = 30.0
#: 每條連線一個可變的容器：兌換 ticket 時填進去，看守的背景工作讀同一個物件
_holder: ContextVar[dict[str, Any] | None] = ContextVar("console_ticket", default=None)


def ticket_fields(request: Any) -> dict[str, Any]:
    """換發 ticket 時要多帶的欄位：開主控台的登入工作階段（API 權杖沒有工作階段 → None）。"""
    sid = getattr(getattr(request, "state", None), "session_id", None)
    return {"sid": str(sid) if sid else None}


def note_ticket(raw: bytes | str | None) -> None:
    """兌換 ticket 時記下是誰、哪個 IP、哪個工作階段（給看守用）。"""
    holder = _holder.get()
    if holder is None or not raw:
        return
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return
    if isinstance(data, dict):
        holder.update({k: data.get(k) for k in ("user_id", "ip_id", "sid")})


async def _permission(kind: str) -> Callable[..., Awaitable[bool]]:
    from app.services import permission as p
    return {
        "ssh": p.can_use_ssh, "sftp": p.can_use_sftp, "rdp": p.can_use_rdp, "vnc": p.can_use_vnc,
        "novnc": p.can_use_novnc, "bmc": p.can_use_bmc, "rustdesk": p.can_use_rustdesk,
    }[kind]


async def revoked_reason(kind: str, user_id: Any, ip_id: Any, sid: Any,
                         started_at: Any = None) -> str | None:
    """還可以繼續用的話回 None；否則回原因。"""
    from app.core.db import SessionLocal
    from app.models.address import IPAddress
    from app.models.user import User
    from app.services.sessions import session_alive

    async with SessionLocal() as s:
        user = await s.get(User, uuid.UUID(str(user_id)))
        if user is None or not user.is_active:
            return "account_inactive"
        if sid:
            if not await session_alive(s, user, sid):
                return "session_revoked"
        elif (user.tokens_valid_after is not None and started_at is not None
              and user.tokens_valid_after > started_at):
            return "session_revoked"      # 用 API 權杖開的：連線之後被強制登出 → 一併結束
        ip = await s.get(IPAddress, uuid.UUID(str(ip_id)))
        if ip is None:
            return "target_removed"
        check = await _permission(kind)
        if not await check(s, user=user, ip=ip):
            return "permission_revoked"
    return None


async def _audit_revoked(kind: str, holder: dict[str, Any], reason: str, peer: str | None) -> None:
    from app.core.audit import append_audit
    from app.core.db import SessionLocal

    async with SessionLocal() as s:
        await append_audit(
            s, actor_user_id=str(holder.get("user_id")), actor_ip=peer, actor_user_agent=None,
            object_type="ip", object_id=str(holder.get("ip_id")), action="console_revoked",
            diff={"console": kind, "reason": reason}, request_id=None)
        await s.commit()


def watch_console(kind: str) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """WebSocket 主控台處理函式的看守。"""

    def deco(fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> None:
            websocket = kwargs.get("websocket") or next((a for a in args if hasattr(a, "accept")), None)
            from datetime import UTC, datetime
            holder: dict[str, Any] = {"started_at": datetime.now(UTC)}
            token = _holder.set(holder)
            me = asyncio.current_task()
            state: dict[str, str | None] = {"reason": None}

            async def watch() -> None:
                while True:
                    await asyncio.sleep(WATCH_INTERVAL)
                    if not holder.get("user_id") or not holder.get("ip_id"):
                        continue          # 還沒兌換 ticket（或 ticket 無效，處理函式自己會關）
                    try:
                        reason = await revoked_reason(kind, holder["user_id"], holder["ip_id"],
                                                      holder.get("sid"), holder.get("started_at"))
                    except Exception as exc:
                        log.warning("console watch check failed (%s): %s", kind, exc)
                        continue
                    if reason is not None:
                        state["reason"] = reason
                        # 先關、再取消：各主控台的 finally 都會自己 `websocket.close()`（1000），
                        # 讓它先關的話瀏覽器收到的是一般關閉，畫面說不出是權限被收回（e2e 實測）
                        with contextlib.suppress(Exception):
                            await websocket.close(code=4403, reason=f"access revoked: {reason}")
                        state["closed"] = "yes"
                        if me is not None:
                            me.cancel()
                        return

            watcher = asyncio.create_task(watch())
            try:
                await fn(*args, **kwargs)
            except asyncio.CancelledError:
                if state["reason"] is None:
                    raise
                if me is not None:
                    me.uncancel()
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher
                _holder.reset(token)
            if state["reason"] is not None:
                peer = getattr(getattr(websocket, "client", None), "host", None)
                if not state.get("closed"):
                    with contextlib.suppress(Exception):
                        await websocket.close(code=4403, reason=f"access revoked: {state['reason']}")
                with contextlib.suppress(Exception):
                    await _audit_revoked(kind, holder, state["reason"], peer)

        wrapper.__console_watch__ = kind  # type: ignore[attr-defined]
        return wrapper

    return deco


async def console_target_blocked(host: str) -> str | None:
    """主控台的目標位址不能是本機、link-local（含雲端中繼資料）、保留位址（core/net_guard 的 console 規則）。

    IP 記錄是有子網路寫入權限的人可以自己建的：不擋的話，建一筆 127.0.0.1 再開主控台，
    就能把 jt-ipam 當跳板連本機的服務。回傳 None＝可以連；否則回原因。"""
    from app.core.net_guard import acheck_target
    from app.core.safe_http import UnsafeOutboundURL
    try:
        await acheck_target(host, policy="console")
    except UnsafeOutboundURL as exc:
        return str(exc)
    return None


async def require_console_target(host: str) -> None:
    """換發主控台 ticket 時用：不能連的目標回 403 並講清楚原因。"""
    from fastapi import HTTPException

    from app.core.ui_error import ui_detail
    reason = await console_target_blocked(host)
    if reason:
        raise HTTPException(status_code=403, detail=ui_detail(
            "console_target_blocked", "這個位址不能開主控台（本機、link-local 或保留位址）", reason=reason))
