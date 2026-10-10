"""開著的主控台跟著權限走（core/console_guard）。

以前主控台只在 WebSocket 打開那一刻檢查帳號與權限；停用帳號、強制登出、收回連線權限之後，
已開著的 SSH／RDP／VNC 照樣用到關掉為止。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import uuid

import pytest
from sqlalchemy import select

from app.core import console_guard
from app.models.address import IPAddress
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.user import User

CONSOLES = ("ssh_console", "sftp_console", "rdp_console", "vnc_console", "novnc_console",
            "bmc_console", "rustdesk_console")


def test_every_console_websocket_is_watched() -> None:
    """新加的主控台如果忘了掛看守、ticket 沒帶工作階段或兌換時沒記下來，這裡擋。"""
    import importlib

    from fastapi.routing import APIWebSocketRoute
    for name in CONSOLES:
        mod = importlib.import_module(f"app.api.v1.endpoints.{name}")
        ws_routes = [r for r in mod.router.routes if isinstance(r, APIWebSocketRoute)]
        assert ws_routes, name
        for r in ws_routes:
            if r.path.endswith("/probe"):
                continue
            assert getattr(r.endpoint, "__console_watch__", None), f"{name}:{r.path} 沒有 @watch_console"
        src = inspect.getsource(mod)
        assert "**ticket_fields(request)" in src, f"{name} 的 ticket 沒帶工作階段"
        assert "note_ticket(raw)" in src, f"{name} 兌換 ticket 時沒有記下來"


class _FakeWS:
    """像 Starlette：只有第一次 close 會送出去，之後再 close 會丟例外（處理函式都包了 suppress）。"""

    def __init__(self) -> None:
        self.closed: tuple[int, str] | None = None
        self.client = type("C", (), {"host": "192.0.2.50"})()

    async def accept(self) -> None:  # 讓 watch_console 認得出這是 websocket
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed is not None:
            raise RuntimeError("Cannot call close once a close message has been sent")
        self.closed = (code, reason)


async def _setup(db_session, *, can_ssh: bool = True):
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.7", state="active", ssh_enabled=True)
    u = User(username=f"c-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@example.invalid",
             password_hash="x", is_active=True, is_admin=True, can_ssh=can_ssh)
    db_session.add_all([ip, u])
    await db_session.commit()
    return u, ip


@pytest.mark.anyio
async def test_deactivation_ends_an_open_console(db_session, monkeypatch) -> None:
    u, ip = await _setup(db_session)
    monkeypatch.setattr(console_guard, "WATCH_INTERVAL", 0.05)
    cleaned = asyncio.Event()

    @console_guard.watch_console("ssh")
    async def handler(websocket, address_id, ticket="") -> None:  # noqa: ANN001
        console_guard.note_ticket(json.dumps({"user_id": str(u.id), "ip_id": str(ip.id), "sid": None}))
        try:
            await asyncio.sleep(30)            # 正在用的主控台
        finally:
            cleaned.set()                      # 既有的收尾（關閉稽核、斷開上游）要照常執行

    ws = _FakeWS()
    task = asyncio.create_task(handler(websocket=ws, address_id=ip.id))
    await asyncio.sleep(0.15)
    assert not task.done(), "權限沒變就不該斷線"
    u.is_active = False
    await db_session.commit()
    await asyncio.wait_for(task, 2)
    assert cleaned.is_set()
    assert ws.closed is not None and ws.closed[0] == 4403
    from app.models.audit import AuditLog
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.action == "console_revoked"))).scalars().all()
    assert rows and rows[-1].diff == {"console": "ssh", "reason": "account_inactive"}


@pytest.mark.anyio
async def test_revoked_session_and_permission_are_detected(db_session) -> None:
    from app.services import sessions
    u, ip = await _setup(db_session)
    issued = await sessions.create_session(db_session, u, None, method="local", mfa=False)
    await db_session.commit()
    sid = issued.session.id
    assert await console_guard.revoked_reason("ssh", u.id, ip.id, sid) is None
    await sessions.revoke(db_session, sid, reason="logout")
    await db_session.commit()
    assert await console_guard.revoked_reason("ssh", u.id, ip.id, sid) == "session_revoked"
    # 權限被收回（非管理員、沒有 can_ssh、也沒有子網路寫入權限）
    u.is_admin = False
    u.can_ssh = False
    await db_session.commit()
    assert await console_guard.revoked_reason("ssh", u.id, ip.id, None) == "permission_revoked"


@pytest.mark.anyio
async def test_normal_close_is_not_reported_as_revoked(db_session, monkeypatch) -> None:
    u, ip = await _setup(db_session)
    monkeypatch.setattr(console_guard, "WATCH_INTERVAL", 0.05)

    @console_guard.watch_console("ssh")
    async def handler(websocket, address_id, ticket="") -> None:  # noqa: ANN001
        console_guard.note_ticket(json.dumps({"user_id": str(u.id), "ip_id": str(ip.id)}))
        await asyncio.sleep(0.12)

    ws = _FakeWS()
    await handler(websocket=ws, address_id=ip.id)
    assert ws.closed is None


@pytest.mark.anyio
async def test_revoked_code_reaches_the_browser_even_if_the_handler_closes_first(db_session, monkeypatch) -> None:
    """實機（e2e，guacd 的 VNC；各主控台的收尾都一樣）：處理函式的 finally 自己先 `websocket.close()`（1000），
    看守之後補的 4403 送不出去 → 畫面只說「連線已中斷」，看不出是權限被收回。"""
    u, ip = await _setup(db_session)
    monkeypatch.setattr(console_guard, "WATCH_INTERVAL", 0.05)

    @console_guard.watch_console("ssh")      # _setup 開的是 SSH；用別種主控台會先因為沒有權限而結束
    async def handler(websocket, address_id, ticket="") -> None:  # noqa: ANN001
        console_guard.note_ticket(json.dumps({"user_id": str(u.id), "ip_id": str(ip.id), "sid": None}))
        try:
            await asyncio.sleep(30)
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()        # 所有主控台的收尾都這樣寫

    ws = _FakeWS()
    task = asyncio.create_task(handler(websocket=ws, address_id=ip.id))
    await asyncio.sleep(0.1)
    u.is_active = False
    await db_session.commit()
    await asyncio.wait_for(task, 2)
    assert ws.closed is not None and ws.closed[0] == 4403, ws.closed
    assert ws.closed[1] == "access revoked: account_inactive"
