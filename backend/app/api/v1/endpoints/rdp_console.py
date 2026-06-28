"""IP 位址 RDP 連線管理：ticket 換發 + WebSocket↔RDP 橋接（瀏覽器 canvas 前端）。

比照 SSH 連線管理（ssh_console.py）的安全架構：
- A01：ticket 與 WS 兩處都重查 `can_use_rdp`（deny-by-default）；看不到的 IP 不能連。
- A07/A09：ticket 單次用 + 60s TTL + 綁 user×ip；發放限流；session 開/關都寫稽核。
- 帳密只在連線過程存記憶體，用完即丟，**絕不寫 DB / 不記錄**；已存帳密走金庫 reference。
- 目標主機固定為該 IP 記錄上的位址（不接受使用者指定 host）→ 防被當成通用 RDP/SSRF proxy。

相依：**aardwolf 為選用**（pin 0.2.13，有 wheel→免 Rust）。未安裝時 `RDP_AVAILABLE=False`，
所有端點回 503、前端隱藏入口。

實作備註（避開 aardwolf 0.2.13 已知 bug，不需 fork / monkeypatch）：
- 輸入直接呼叫 `conn.send_mouse` / `conn.send_key_*`（單一 pump_in 協程序列送出），
  不走 `ext_in_queue`（其 `__external_reader` 傳給 send_mouse 的 wheel steps 恆 0）。
- 滾輪一律用 `MOUSEBUTTON_WHEEL_UP` 並把方向放進 steps：向下 = `0x100`(WHEEL_NEGATIVE 位) | 量值，
  讓 WHEEL_UP 分支自動帶上 `PTRFLAGS.WHEEL`（修掉 WHEEL_DOWN 漏設 WHEEL flag 的 bug）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser
from app.core.audit import append_audit
from app.core.config import get_settings
from app.core.db import SessionLocal, get_session
from app.core.rate_limit import _redis_client
from app.core.security import envelope_decrypt
from app.models.address import IPAddress
from app.models.device import Device
from app.models.ssh_credential import SSHCredential
from app.models.user import User
from app.schemas.address import IPAddressRead
from app.services.permission import (
    can_use_rdp,
    get_object_permission,
    has_permission,
    visible_ids,
)

try:  # aardwolf 為選用相依（pin 0.2.13）；未裝則 RDP 功能停用
    from aardwolf.commons.factory import RDPConnectionFactory
    from aardwolf.commons.iosettings import RDPIOSettings
    from aardwolf.commons.queuedata import RDPDATATYPE
    from aardwolf.commons.queuedata.constants import MOUSEBUTTON, VIDEO_FORMAT

    RDP_AVAILABLE = True
except Exception:  # 任何 import 問題都視為未安裝
    RDP_AVAILABLE = False

router = APIRouter(prefix="/addresses", tags=["rdp"])

_TICKET_TTL = 60              # 秒；ticket 單次用、短壽
_CONNECT_TIMEOUT = 20.0       # RDP（NLA）連線逾時
_CLIENT_IDLE_TIMEOUT = 60.0   # WS 端 60s 無任何訊息（含 heartbeat）視為斷線
_WHEEL_DELTA = 120            # 一格滾輪
_WHEEL_NEGATIVE = 0x100       # PTRFLAGS.WHEEL_NEGATIVE 位（放進 steps 表向下）
_MAX_DIM = 2560              # 解析度上限保護

# 鍵盤特殊鍵 → (PC set-1 scancode, is_extended)
_SPECIAL_KEYS: dict[str, tuple[int, bool]] = {
    "Enter": (0x1C, False), "Backspace": (0x0E, False), "Tab": (0x0F, False),
    "Escape": (0x01, False), "Delete": (0x53, True), "Home": (0x47, True),
    "End": (0x4F, True), "PageUp": (0x49, True), "PageDown": (0x51, True),
    "Insert": (0x52, True), "ArrowUp": (0x48, True), "ArrowDown": (0x50, True),
    "ArrowLeft": (0x4B, True), "ArrowRight": (0x4D, True),
    "Control": (0x1D, False), "Shift": (0x2A, False), "Alt": (0x38, False),
    "Meta": (0x5B, True), " ": (0x39, False),  # Meta = 左 Windows 鍵（extended）
    "F1": (0x3B, False), "F2": (0x3C, False), "F3": (0x3D, False), "F4": (0x3E, False),
    "F5": (0x3F, False), "F6": (0x40, False), "F7": (0x41, False), "F8": (0x42, False),
    "F9": (0x43, False), "F10": (0x44, False), "F11": (0x57, False), "F12": (0x58, False),
}

# 同時在線 session 計數（單核 GIL 下限制並發；0 = 不限）
_active_sessions = 0


def _ticket_key(ticket: str) -> str:
    return f"rdp:tk:{ticket}"


def _mouse_button(b: int) -> Any:
    return {0: MOUSEBUTTON.MOUSEBUTTON_LEFT, 1: MOUSEBUTTON.MOUSEBUTTON_RIGHT,
            2: MOUSEBUTTON.MOUSEBUTTON_MIDDLE}.get(int(b), MOUSEBUTTON.MOUSEBUTTON_LEFT)


@router.get("/connections/targets", response_model=list[IPAddressRead])
async def list_connection_targets(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[IPAddressRead]:
    """列出所有已啟用 SSH 或 RDP、且目前使用者可連線的 IP（進階→連線管理頁用）。

    與 can_use_ssh/can_use_rdp 一致的 deny-by-default：admin 全部；否則限可見子網路，
    再依「對該子網路有 write」或「具 can_ssh 能力且至少 read」逐筆放行。每筆回 ssh/rdp 兩旗標。
    """
    stmt = select(IPAddress).where(
        IPAddress.ssh_enabled.is_(True)
        | IPAddress.rdp_enabled.is_(True)
        | IPAddress.vnc_enabled.is_(True)
        | IPAddress.novnc_enabled.is_(True)
    )
    if not user.is_admin:
        vis = await visible_ids(session, user=user, object_type="subnet")
        if vis is not None:
            if not vis:
                return []
            stmt = stmt.where(IPAddress.subnet_id.in_(vis))
    rows = (await session.execute(stmt)).scalars().all()

    perm_cache: dict[uuid.UUID, str] = {}
    kept: list[tuple[IPAddress, bool, bool, bool]] = []
    for ip in rows:
        if user.is_admin:
            usable = True
        else:
            lvl = perm_cache.get(ip.subnet_id)
            if lvl is None:
                lvl = await get_object_permission(
                    session, user=user, object_type="subnet", object_id=ip.subnet_id
                )
                perm_cache[ip.subnet_id] = lvl
            if lvl == "none":
                continue
            usable = has_permission(lvl, "write") or bool(user.can_ssh)
        if not usable:
            continue
        kept.append((ip, bool(ip.ssh_enabled), bool(ip.rdp_enabled), bool(ip.vnc_enabled)))

    dev_ids = {ip.device_id for ip, _, _, _ in kept if ip.device_id}
    dev_names: dict[uuid.UUID, str] = {}
    if dev_ids:
        drows = (await session.execute(
            select(Device.id, Device.name).where(Device.id.in_(dev_ids))
        )).all()
        dev_names = {d[0]: d[1] for d in drows}

    from app.services.os_precedence import effective_os
    out: list[IPAddressRead] = []
    for ip, ssh_ok, rdp_ok, vnc_ok in kept:
        r = IPAddressRead.model_validate(ip)
        r.ssh_available = ssh_ok
        r.rdp_available = rdp_ok
        r.vnc_available = vnc_ok
        if ip.novnc_enabled:  # PVE 主控台：已啟用且對應到 PVE VM/CT（權限已在 kept 過濾）
            from app.services.pve_console import resolve_pve_target
            tgt = await resolve_pve_target(session, ip)
            if tgt is not None:
                r.novnc_available = True
                from app.schemas.address import PveConsoleTarget
                r.pve = PveConsoleTarget(kind=tgt.kind, node=tgt.node, vmid=tgt.vmid, cluster=tgt.cluster_name)
        r.device_name = dev_names.get(ip.device_id) if ip.device_id else None
        # OS 與 IP 詳細資料頁一致：依來源優先序（librenms/wazuh/scanner）解析有效值
        _os = await effective_os(session, ip)
        r.os_guess = _os["os_guess"]; r.os_family = _os["os_family"]; r.os_source = _os["os_source"]
        out.append(r)
    return out


@router.post("/{address_id}/rdp/ticket")
async def issue_rdp_ticket(
    address_id: uuid.UUID,
    user: CurrentUser,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """換發短期一次性 ticket；之後用它開 WebSocket。"""
    if not RDP_AVAILABLE:
        raise HTTPException(status_code=503, detail="RDP 功能未安裝（缺 aardwolf 選用相依）")
    from app.core.rate_limit import limit_per_ip

    await limit_per_ip(request, name="rdp")

    ip = await session.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=404, detail="Address not found")
    if not await can_use_rdp(session, user=user, ip=ip):
        raise HTTPException(status_code=403, detail="無 RDP 連線權限")

    saved = (await session.execute(
        select(SSHCredential.id).where(
            SSHCredential.owner_user_id == user.id,
            SSHCredential.protocol == "rdp",
            (SSHCredential.target_ip_id == ip.id) | (SSHCredential.target_ip_id.is_(None)),
        ).limit(1)
    )).first()

    from app.services.system_config import get_rdp_clipboard_paste
    clip_enabled = await get_rdp_clipboard_paste(session)

    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"user_id": str(user.id), "ip_id": str(ip.id)})
    await _redis_client().set(_ticket_key(ticket), payload, ex=_TICKET_TTL)

    return {
        "ticket": ticket,
        "ws_path": f"/api/v1/addresses/{ip.id}/rdp/ws",
        "default_size": {"width": 1280, "height": 800},
        "has_saved_creds": saved is not None,
        "clipboard_paste": clip_enabled,
        "ttl": _TICKET_TTL,
    }


async def _redeem_ticket(ticket: str, address_id: uuid.UUID) -> uuid.UUID | None:
    if not ticket:
        return None
    raw = await _redis_client().getdel(_ticket_key(ticket))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if data.get("ip_id") != str(address_id):
            return None
        return uuid.UUID(data["user_id"])
    except (ValueError, KeyError, TypeError):
        return None


async def _audit_rdp(
    *, actor_user_id: str, actor_ip: str | None, object_id: str,
    action: str, diff: dict[str, Any],
) -> None:
    async with SessionLocal() as s:
        await append_audit(
            s, actor_user_id=actor_user_id, actor_ip=actor_ip, actor_user_agent=None,
            object_type="ip", object_id=object_id, action=action, diff=diff, request_id=None,
        )
        await s.commit()


@router.websocket("/{address_id}/rdp/ws")
async def rdp_ws(websocket: WebSocket, address_id: uuid.UUID, ticket: str = "") -> None:
    global _active_sessions

    if not RDP_AVAILABLE:
        await websocket.close(code=4503)
        return

    # 1) 驗 ticket（單次取出）
    user_id = await _redeem_ticket(ticket, address_id)
    if user_id is None:
        await websocket.close(code=4401)
        return

    # 2) 載入 user + ip，縱深重查權限
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        ip = await s.get(IPAddress, address_id)
        if user is None or not user.is_active or ip is None:
            await websocket.close(code=4403)
            return
        allowed = await can_use_rdp(s, user=user, ip=ip)
        host = str(ip.ip).split("/")[0]
        from app.services.system_config import get_rdp_clipboard_paste
        clip_enabled = await get_rdp_clipboard_paste(s)
    if not allowed:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    actor_ip = websocket.client.host if websocket.client else None

    async def send(obj: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(obj))

    # 並發上限（避免單核被多 session 拖垮）
    cap = get_settings().rdp_max_sessions
    if cap and _active_sessions >= cap:
        await send({"type": "error", "code": "too_many", "message": f"RDP 同時連線已達上限（{cap}）"})
        await websocket.close()
        return

    conn = None
    counted = False
    started: datetime | None = None
    try:
        # 3) 收第一個設定訊息
        cfg = json.loads(await websocket.receive_text())
        if cfg.get("type") != "config":
            await send({"type": "error", "code": "bad_config", "message": "缺少連線設定"})
            await websocket.close()
            return
        width = max(640, min(_MAX_DIM, int(cfg.get("width") or 1280)))
        height = max(480, min(_MAX_DIM, int(cfg.get("height") or 800)))
        username = (cfg.get("username") or "").strip()
        password = cfg.get("password") or ""
        domain = (cfg.get("domain") or "").strip()
        credential_id = cfg.get("credential_id")

        # 4) 已存帳密（金庫）— 明文只在記憶體
        used_cred_id: uuid.UUID | None = None
        if credential_id:
            from app.api.v1.endpoints.ssh_credentials import cred_aad
            async with SessionLocal() as s:
                try:
                    cred = await s.get(SSHCredential, uuid.UUID(str(credential_id)))
                except ValueError:
                    cred = None
                if (cred is None or cred.owner_user_id != user_id or cred.protocol != "rdp"
                        or (cred.target_ip_id is not None and str(cred.target_ip_id) != str(address_id))):
                    await send({"type": "error", "code": "cred_not_found", "message": "找不到可用的已存帳密"})
                    await websocket.close()
                    return
                used_cred_id = cred.id
                username = cred.username
                domain = cred.domain or ""
                secrets_enc = dict(cred.secrets_enc or {})
            try:
                password = envelope_decrypt(secrets_enc["password"], aad=cred_aad(user_id, "password"))
            except Exception:
                await send({"type": "error", "code": "bad_key", "message": "已存帳密解密失敗"})
                await websocket.close()
                return
            async with SessionLocal() as s:
                c2 = await s.get(SSHCredential, used_cred_id)
                if c2 is not None:
                    c2.last_used_at = datetime.now(UTC)
                    await s.commit()
        if not username:
            await send({"type": "error", "code": "bad_config", "message": "帳號必填"})
            await websocket.close()
            return

        # 5) 建立 RDP 連線（NLA / CredSSP+NTLM）
        await send({"type": "status", "state": "connecting"})
        io = RDPIOSettings()
        # 預設不啟用任何虛擬通道；僅在管理者開啟「控制端貼上」時才掛剪貼簿通道（cliprdr）
        if clip_enabled:
            from aardwolf.extensions.RDPECLIP.channel import RDPECLIPChannel
            io.channels = [RDPECLIPChannel]
        else:
            io.channels = []
        io.video_width = width
        io.video_height = height
        io.video_bpp_min = 24
        io.video_bpp_max = 32
        io.video_out_format = VIDEO_FORMAT.PNG
        io.clipboard_use_pyperclip = False

        user_in_url = quote(f"{domain}\\{username}" if domain else username, safe="")
        b64pw = base64.b64encode(password.encode("utf-8")).decode("ascii")
        url = f"rdp+ntlm-pwb64://{user_in_url}:{b64pw}@{host}/?timeout={int(_CONNECT_TIMEOUT)}"
        del password, b64pw

        factory = RDPConnectionFactory.from_url(url, io)
        conn = factory.create_connection_newtarget(host, io)
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT):
                _result, err = await conn.connect()
        except TimeoutError:
            await send({"type": "error", "code": "connect_failed", "message": "連線逾時"})
            await websocket.close()
            return
        if err is not None:
            # NLA 認證失敗或連線錯誤 — 不回堆疊細節
            await send({"type": "error", "code": "auth_failed",
                        "message": "連線/認證失敗（帳號、密碼、網域或 NLA 設定）"})
            await websocket.close()
            return

        _active_sessions += 1
        counted = True
        started = datetime.now(UTC)
        await _audit_rdp(
            actor_user_id=str(user_id), actor_ip=actor_ip, object_id=str(address_id),
            action="rdp.session_open",
            diff={"host": host, "username": username, "domain": domain or None,
                  "size": f"{width}x{height}",
                  "credential_id": str(used_cred_id) if used_cred_id else None},
        )
        await send({"type": "status", "state": "connected", "width": width, "height": height})

        await _bridge(websocket, conn, send, clip_enabled=clip_enabled)

    except WebSocketDisconnect:
        pass
    except Exception:  # 不洩漏堆疊
        with contextlib.suppress(Exception):
            await send({"type": "error", "code": "internal", "message": "連線發生未預期錯誤"})
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.terminate()
        if counted:
            _active_sessions -= 1
            if started is not None:
                dur = (datetime.now(UTC) - started).total_seconds()
                with contextlib.suppress(Exception):
                    await _audit_rdp(
                        actor_user_id=str(user_id), actor_ip=actor_ip, object_id=str(address_id),
                        action="rdp.session_close", diff={"host": host, "duration_seconds": round(dur, 1)},
                    )
        with contextlib.suppress(Exception):
            await send({"type": "status", "state": "disconnected"})
            await websocket.close()


async def _bridge(websocket: WebSocket, conn: Any, send: Any, *, clip_enabled: bool = False) -> None:
    """雙向 pump：RDP 視訊→ws（PNG tile）、ws→直接呼叫 send_mouse/send_key。

    clip_enabled 時額外接受 {type:"clip", text} → 單向把文字塞進被控端剪貼簿（控制端→被控端）。
    伺服器→控制端的剪貼簿一律不回傳（pump_out 只送視訊），維持單向、不外洩被控端剪貼簿。
    """

    async def pump_out() -> None:
        with contextlib.suppress(Exception):
            while True:
                data = await conn.ext_out_queue.get()
                if data is None:
                    break
                if getattr(data, "type", None) == RDPDATATYPE.VIDEO and data.data:
                    await send({
                        "type": "img", "x": data.x, "y": data.y,
                        "w": data.width, "h": data.height,
                        "d": base64.b64encode(data.data).decode("ascii"),
                    })

    async def pump_in() -> None:
        with contextlib.suppress(WebSocketDisconnect, Exception):
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=_CLIENT_IDLE_TIMEOUT)
                except TimeoutError:
                    break
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "m":
                    x, y = int(msg.get("x", 0)), int(msg.get("y", 0))
                    if msg.get("wheel"):
                        steps = _WHEEL_DELTA + (_WHEEL_NEGATIVE if int(msg.get("dir", -1)) < 0 else 0)
                        await conn.send_mouse(MOUSEBUTTON.MOUSEBUTTON_WHEEL_UP, x, y, False, steps)
                    elif msg.get("move"):
                        await conn.send_mouse(MOUSEBUTTON.MOUSEBUTTON_HOVER, x, y, False)
                    else:
                        await conn.send_mouse(_mouse_button(msg.get("b", 0)), x, y, bool(msg.get("p")))
                elif t == "k":
                    pressed = bool(msg.get("p"))
                    key = msg.get("key", "")
                    if key in _SPECIAL_KEYS:
                        sc, ext = _SPECIAL_KEYS[key]
                        await conn.send_key_scancode(sc, pressed, ext)
                    else:
                        ch = msg.get("ch", "")
                        if len(ch) == 1:
                            await conn.send_key_char(ch, pressed)
                elif t == "clip":
                    # 控制端貼上：把文字寫進被控端剪貼簿（單向、純文字、長度上限 100k）
                    if clip_enabled:
                        text = str(msg.get("text", ""))[:100000]
                        if text:
                            with contextlib.suppress(Exception):
                                await conn.set_current_clipboard_text(text)
                elif t == "ping":
                    await send({"type": "pong"})
                elif t == "close":
                    break

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    _done, pending = await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
