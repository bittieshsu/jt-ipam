"""主控台的連線出口：直連，或經由一台 SSH 跳板（issue #24 階段一）。

六個主控台（ssh / sftp / rdp / vnc / novnc / bmc）本來都是後端**直接**連目標 IP。
客戶站台若只能經由自己的跳板抵達，直連就到不了 —— 而且多個客戶用相同的私網網段時，
單看 IP 字串根本分不出要走哪一條路。

**歧義其實已被結構解決**：主控台是從一筆 IP 記錄啟動的，而每筆 IP 必然屬於唯一一個子網路，
所以把出口掛在子網路（IP 可覆寫）上天生不會弄錯。

用法（六個主控台共用同一個接縫）：

    route = await resolve_route(session, ip)
    async with dial(route, host, port) as (host, port):
        ...            # host/port 可能已被換成 127.0.0.1:<本機轉發埠>

`dial()` 對 `Direct` 是零成本的：原樣 yield 回去，不會建立任何連線。

## 為什麼不直接用 `ssh_tunnel.open_tunnel()`

規格原本寫「複用 `open_tunnel()`」，但它每呼叫一次就**開一條新的 SSH 連線**，
而這裡要求「同一跳板的多個 session 共用一條連線」。兩者不相容，所以這裡自己管連線池，
但**沿用 `ssh_tunnel` 的安全零件**：`LEGACY_SSH_ALGS`（老舊網路裝置的相容演算法）、
host key 指紋計算與 `SSHHostKeyMismatch`。安全行為因此與既有的 SSH 通道一致。

## 這裡刻意不做的事

- **不接受呼叫端指定跳板**：出口只從資料庫的指派推導。否則主控台就會退化成
  「可以連任何地方的通用 proxy」，那正是原本每個主控台都特意防掉的（見各檔開頭）。
- **host key 沒釘選就不連**：跳板是整條路徑的中間人，這一步不能省。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncssh
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.models.address import IPAddress
from app.models.jump_host import JumpHost
from app.models.subnet import Subnet
from app.services.ssh_tunnel import (
    LEGACY_SSH_ALGS,
    SSHHostKeyMismatch,
    SSHTunnelError,
    server_key_fingerprint_sha256,
)

#: 建立跳板連線的逾時。比主控台本身的逾時短 —— 跳板不通時要快點講，
#: 不要讓使用者盯著一個沒有回應的終端機。
CONNECT_TIMEOUT = 15.0


class JumpHostError(RuntimeError):
    """跳板連線失敗。訊息會直接顯示給使用者，要說得出原因。"""


@dataclass(frozen=True)
class Direct:
    """不經跳板。"""


@dataclass(frozen=True)
class ViaJumpHost:
    """經由這台跳板。只帶連線需要的欄位，不把整個 ORM 物件拖進 WS 生命週期。"""

    id: uuid.UUID
    name: str
    host: str
    port: int
    username: str
    auth_kind: str
    secret: str                    # 私鑰 PEM 或密碼（已解密，僅存在記憶體）
    host_key_fingerprint: str | None
    max_sessions: int


Route = Direct | ViaJumpHost


# ─────────────────── 機密欄位 ───────────────────
def _aad(jump_id: uuid.UUID, field: str) -> bytes:
    return f"jump_host:{jump_id}:{field}".encode()


def encrypt_secret_for(jump_id: uuid.UUID, field: str, value: str) -> tuple[bytes, bytes]:
    return encrypt_secret(value, aad=_aad(jump_id, field))


def _decrypt(jump: JumpHost) -> str:
    field = "private_key" if jump.auth_kind == "key" else "password"
    enc = jump.private_key_enc if field == "private_key" else jump.password_enc
    nonce = jump.private_key_nonce if field == "private_key" else jump.password_nonce
    if not enc or not nonce:
        raise JumpHostError(f"跳板「{jump.name}」還沒有設定{'金鑰' if field == 'private_key' else '密碼'}")
    return decrypt_secret(enc, nonce, aad=_aad(jump.id, field)).decode("utf-8")


# ─────────────────── 解析 ───────────────────
async def resolve_route(session: AsyncSession, ip: IPAddress) -> Route:
    """這筆 IP 的主控台該走哪條路：**IP 覆寫 > 子網路 > 直連**。

    停用中的跳板視同沒有指派 → 直連。**刻意不是報錯**：停用是管理動作，
    不應該讓一整批主控台變成無法連線；真的要擋掉連線就把 IP 的可見性收掉。
    """
    jump_id = ip.jump_host_id
    if jump_id is None and ip.subnet_id is not None:
        subnet = await session.get(Subnet, ip.subnet_id)
        jump_id = subnet.jump_host_id if subnet is not None else None
    if jump_id is None:
        return Direct()

    jump = await session.get(JumpHost, jump_id)
    if jump is None or not jump.enabled:
        return Direct()
    return ViaJumpHost(
        id=jump.id, name=jump.name, host=jump.host, port=jump.port,
        username=jump.username, auth_kind=jump.auth_kind, secret=_decrypt(jump),
        host_key_fingerprint=jump.host_key_fingerprint,
        max_sessions=jump.max_sessions,
    )


def route_label(route: Route) -> str | None:
    """稽核與 UI 用：經由哪個跳板（直連回 None）。"""
    return route.name if isinstance(route, ViaJumpHost) else None


# ─────────────────── 連線池 ───────────────────
@dataclass
class _Pooled:
    conn: asyncssh.SSHClientConnection
    refs: int = 0


#: jump_host_id → 共用連線。多個 session 共用一條 SSH 連線（asyncssh 一條連線可開多個轉發），
#: 最後一個 session 離開就關掉 —— 不留閒置連線掛在客戶的跳板上。
_pool: dict[uuid.UUID, _Pooled] = {}
_pool_lock = asyncio.Lock()


def _client_factory(expected_fp: str) -> type[asyncssh.SSHClient]:
    """比對釘選指紋；不符就丟 `SSHHostKeyMismatch`（＝可能有中間人）。"""

    class _Strict(asyncssh.SSHClient):
        def validate_host_public_key(self, host, addr, port, key):  # type: ignore[no-untyped-def]
            b64 = key.export_public_key("openssh").decode("ascii").split()[1]
            actual = server_key_fingerprint_sha256(base64.b64decode(b64))
            if actual != expected_fp:
                raise SSHHostKeyMismatch(expected_fp, actual)
            return True

    return _Strict


async def _connect(jump: ViaJumpHost) -> asyncssh.SSHClientConnection:
    if not jump.host_key_fingerprint:
        # 沒釘選就連＝接受任何 host key，而跳板是整條路徑的中間人。
        # 指紋要在管理頁按「測試連線」時取回並確認。
        raise JumpHostError(
            f"跳板「{jump.name}」尚未信任主機金鑰：請先到管理頁按「測試連線」核對指紋",
        )
    opts: dict[str, Any] = {
        "username": jump.username,
        "client_factory": _client_factory(jump.host_key_fingerprint),
        "known_hosts": None,
        "agent_path": None,          # 不繼承 ssh-agent
        **LEGACY_SSH_ALGS,
    }
    if jump.auth_kind == "key":
        try:
            opts["client_keys"] = [asyncssh.import_private_key(jump.secret)]
        except Exception as exc:
            raise JumpHostError(f"跳板「{jump.name}」的私鑰無法解析：{exc}") from exc
        opts["preferred_auth"] = ("publickey",)
    else:
        opts["password"] = jump.secret
        opts["client_keys"] = []      # 不要讓 asyncssh 去翻本機的 ~/.ssh
        opts["preferred_auth"] = ("keyboard-interactive", "password")

    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            return await asyncssh.connect(jump.host, port=jump.port, **opts)
    except SSHHostKeyMismatch as exc:
        raise JumpHostError(
            f"跳板「{jump.name}」的主機金鑰與釘選的不符（可能遭中間人攔截）：{exc}",
        ) from exc
    except TimeoutError as exc:
        raise JumpHostError(
            f"連跳板「{jump.name}」（{jump.host}:{jump.port}）逾時 {CONNECT_TIMEOUT:.0f} 秒",
        ) from exc
    except asyncssh.PermissionDenied as exc:
        raise JumpHostError(f"跳板「{jump.name}」認證失敗：{exc}") from exc
    except (asyncssh.Error, OSError) as exc:
        # 帶上底層原文：ConnectError 一個名字底下有 DNS／拒絕／路由不通好幾種，
        # 少了原因就只能猜（與 core/safe_http.transport_detail 同一條原則）
        raise JumpHostError(
            f"連不上跳板「{jump.name}」（{jump.host}:{jump.port}）："
            f"{exc.__class__.__name__}: {exc}",
        ) from exc


@dataclass
class Tunnel:
    """要連的位址，外加「用完要還」。

    直連時 `aclose()` 什麼都不做 —— 呼叫端因此不必分辨自己走的是哪一條路。
    做成命令式而不是只有 context manager：六個主控台的連線是一大段既有的
    try/except，包成 `async with` 得整段重新縮排，那種改法最容易在協定層改錯東西。
    """

    host: str
    port: int
    via: str | None = None                 # 經由哪個跳板（稽核與 UI 用）
    _listener: Any = None
    _jump_id: uuid.UUID | None = None
    _closed: bool = False

    async def aclose(self) -> None:
        if self._closed:
            return                          # aclose() 要可以被呼叫兩次（finally 疊 finally）
        self._closed = True
        if self._listener is not None:
            self._listener.close()
        if self._jump_id is None:
            return
        async with _pool_lock:
            pooled = _pool.get(self._jump_id)
            if pooled is None:
                return
            pooled.refs -= 1
            if pooled.refs <= 0:
                _pool.pop(self._jump_id, None)
                pooled.conn.close()


async def open_route(route: Route, host: str, port: int) -> Tunnel:
    """取得可連的位址。**呼叫端必須在 finally 裡 `await tunnel.aclose()`。**"""
    if isinstance(route, Direct):
        return Tunnel(host=host, port=port)

    async with _pool_lock:
        pooled = _pool.get(route.id)
        if pooled is None:
            conn = await _connect(route)
            pooled = _Pooled(conn=conn)
            _pool[route.id] = pooled
        elif pooled.refs >= route.max_sessions:
            raise JumpHostError(
                f"跳板「{route.name}」同時連線數已達上限 {route.max_sessions}，請稍後再試",
            )
        pooled.refs += 1

    tunnel = Tunnel(host="", port=0, via=route.name, _jump_id=route.id)
    try:
        listener = await pooled.conn.forward_local_port("127.0.0.1", 0, host, port)
    except (asyncssh.Error, OSError) as exc:
        await tunnel.aclose()               # 還沒轉發成功也要把 refs 還回去
        raise JumpHostError(
            f"跳板「{route.name}」無法轉發到 {host}:{port}："
            f"{exc.__class__.__name__}: {exc}",
        ) from exc
    tunnel.host, tunnel.port, tunnel._listener = "127.0.0.1", listener.get_port(), listener
    return tunnel


@contextlib.asynccontextmanager
async def dial(route: Route, host: str, port: int) -> AsyncIterator[tuple[str, int]]:
    """`open_route()` 的 context manager 版本（新程式碼與測試用）。"""
    tunnel = await open_route(route, host, port)
    try:
        yield tunnel.host, tunnel.port
    finally:
        await tunnel.aclose()


async def probe(jump: JumpHost) -> dict[str, Any]:
    """管理頁的「測試連線」：取回主機金鑰指紋，並在已釘選時實際登入一次。

    未釘選時**只取指紋、不登入** —— 指紋還沒被人確認之前，把帳密送過去就已經太遲了。
    """
    from app.services.ssh_tunnel import fetch_host_key

    out: dict[str, Any] = {"host": jump.host, "port": jump.port}
    try:
        hk = await fetch_host_key(jump.host, port=jump.port, timeout=CONNECT_TIMEOUT)
    except SSHTunnelError as exc:
        raise JumpHostError(f"連不上跳板：{exc}") from exc
    out["fingerprint"] = hk["fingerprint"]
    out["pinned"] = jump.host_key_fingerprint
    out["matches"] = (jump.host_key_fingerprint == hk["fingerprint"]
                      if jump.host_key_fingerprint else None)

    if not jump.host_key_fingerprint:
        out["authenticated"] = False
        out["note"] = "尚未釘選主機金鑰：請核對指紋後按「信任並儲存」，之後才會實際登入測試"
        return out
    if out["matches"] is False:
        raise JumpHostError(
            f"主機金鑰與釘選的不符（可能遭中間人攔截）：釘選 {jump.host_key_fingerprint}，"
            f"實際 {hk['fingerprint']}",
        )

    route = ViaJumpHost(
        id=jump.id, name=jump.name, host=jump.host, port=jump.port,
        username=jump.username, auth_kind=jump.auth_kind, secret=_decrypt(jump),
        host_key_fingerprint=jump.host_key_fingerprint, max_sessions=jump.max_sessions,
    )
    conn = await _connect(route)
    try:
        out["authenticated"] = True
        out["server_version"] = getattr(conn, "get_extra_info", lambda *_: None)("server_version")
    finally:
        conn.close()
    out["checked_at"] = datetime.now(UTC).isoformat()
    return out
