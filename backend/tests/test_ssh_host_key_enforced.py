"""釘選的 SSH 主機金鑰要真的被比對（2026-10-12）。

asyncssh 收到 known_hosts=None 是「整個關掉主機金鑰驗證」，連 SSHClient.validate_host_public_key
都不會被呼叫。內建 SSH 主控台、跳板、SSH 通道原本都是「known_hosts=None＋自訂嚴格回呼」：
看起來有比對，實際上任何金鑰都會被接受，帳密直接交給冒充的主機。SFTP 主控台則是完全沒驗。
e2e 把釘選的金鑰換成錯的，結果直接走到「認證失敗」才發現。

這裡在程序內起一台 SSH 伺服器（不需要外部主機），每一條連線路徑都拿錯的金鑰連一次、
對的金鑰連一次；另外靜態掃描整個 app，不可以再出現 known_hosts=None（例外要標記理由）。
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import asyncssh
import pytest
from app.services import ssh_tunnel
from app.services.ssh_tunnel import SSHHostKeyMismatch

pytestmark = pytest.mark.anyio

_APP = Path(__file__).resolve().parent.parent / "app"


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return password == "pw"

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return True


def _line(key: asyncssh.SSHKey) -> str:
    return " ".join(key.export_public_key("openssh").decode().split()[:2])


@pytest.fixture
async def sshd(monkeypatch):  # type: ignore[no-untyped-def]
    """回傳 (port, 伺服器金鑰那一行, 另一把不相干的金鑰那一行)。"""
    key = asyncssh.generate_private_key("ssh-ed25519")
    other = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(_Server, "127.0.0.1", 0, server_host_keys=[key])
    port = server.sockets[0].getsockname()[1]

    async def _allow(host: str, port: int) -> None:  # 測試靶在 127.0.0.1
        return None
    monkeypatch.setattr(ssh_tunnel, "guard_ssh_target", _allow)
    yield port, _line(key), _line(other)
    server.close()
    await server.wait_closed()


def test_pinned_known_hosts_never_disables_verification() -> None:
    assert ssh_tunnel.pinned_known_hosts(None) == ([], [], [])
    assert ssh_tunnel.pinned_known_hosts("") == ([], [], [])
    k = asyncssh.generate_private_key("ssh-ed25519")
    trusted, ca, revoked = ssh_tunnel.pinned_known_hosts(_line(k))
    assert [_line(x) for x in trusted] == [_line(k)]
    assert ca == []
    assert revoked == []


async def test_ssh_console_builtin_engine(sshd) -> None:  # type: ignore[no-untyped-def]
    from app.api.v1.endpoints.ssh_console import _connect_builtin
    port, right, wrong = sshd
    with pytest.raises(SSHHostKeyMismatch):
        await _connect_builtin("127.0.0.1", port, "u", wrong, {"password": "pw"})
    conn = await _connect_builtin("127.0.0.1", port, "u", right, {"password": "pw"})
    conn.close()


async def test_sftp_console(sshd) -> None:  # type: ignore[no-untyped-def]
    from app.api.v1.endpoints.sftp_console import _connect_pinned
    port, right, wrong = sshd
    with pytest.raises(SSHHostKeyMismatch):
        await _connect_pinned("127.0.0.1", port, "u", wrong, {"password": "pw"})
    conn = await _connect_pinned("127.0.0.1", port, "u", right, {"password": "pw"})
    conn.close()


async def test_jump_host_fingerprint(sshd) -> None:  # type: ignore[no-untyped-def]
    from app.services import console_route
    port, right, wrong = sshd

    def jump(line: str) -> console_route.ViaJumpHost:
        return console_route.ViaJumpHost(
            id=uuid.uuid4(), name="j", host="127.0.0.1", port=port, username="u", auth_kind="password",
            secret="pw", host_key_fingerprint=ssh_tunnel.fingerprint_of_line(line), max_sessions=1)

    with pytest.raises(console_route.JumpHostError) as ei:
        await console_route._connect(jump(wrong))
    assert ei.value.code == "jump_host_key_changed"
    conn = await console_route._connect(jump(right))
    conn.close()


async def test_ssh_tunnel(sshd) -> None:  # type: ignore[no-untyped-def]
    port, right, wrong = sshd
    pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()

    def cfg(line: str) -> ssh_tunnel.TunnelConfig:
        return ssh_tunnel.TunnelConfig(host="127.0.0.1", port=port, username="u", private_key_pem=pem,
                                       known_host=line, remote_host="127.0.0.1", remote_port=9)

    with pytest.raises(SSHHostKeyMismatch):
        async with ssh_tunnel.open_tunnel(cfg(wrong)):
            pass
    async with ssh_tunnel.open_tunnel(cfg(right)) as local_port:
        assert local_port > 0



def _none_known_hosts(src: str) -> list[tuple[int, str]]:
    """原始碼裡把 known_hosts 設成 None 的地方（呼叫的關鍵字參數或 dict 字面值）；
    同一行標了 `host-key: unverified-ok` 的不算。看程式本身（AST），註解與說明文字不算。"""
    lines = src.splitlines()
    out = []
    for node in ast.walk(ast.parse(src)):
        hits: list[ast.expr] = []
        if isinstance(node, ast.Call):
            hits = [k.value for k in node.keywords if k.arg == "known_hosts"]
        elif isinstance(node, ast.Dict):
            hits = [v for k, v in zip(node.keys, node.values, strict=True)
                    if isinstance(k, ast.Constant) and k.value == "known_hosts"]
        for v in hits:
            line = lines[v.lineno - 1]
            if isinstance(v, ast.Constant) and v.value is None and "host-key: unverified-ok" not in line:
                out.append((v.lineno, line.strip()))
    return out


def test_no_known_hosts_none_left_in_app() -> None:
    """known_hosts=None 一律禁止；真的要不驗證（只剩 phpIPAM 搬移的選填）就在同一行標
    `host-key: unverified-ok` 並寫理由。"""
    bad = [f"{f.relative_to(_APP)}:{n}: {line}"
           for f in _APP.rglob("*.py") for n, line in _none_known_hosts(f.read_text(encoding="utf-8"))]
    assert not bad, "known_hosts=None 會讓 asyncssh 完全不驗主機金鑰：\n" + "\n".join(bad)


def test_guard_catches_both_spellings() -> None:
    """守門本身要抓得到東西：關鍵字參數、dict 字面值都要抓；標記過的、說明文字裡的不算。"""
    sample = "\n".join([
        "asyncssh.connect(h, known_hosts=None)",
        "opts = {'known_hosts': None}",
        "opts = {'known_hosts': None}  # host-key: unverified-ok 理由",
        "doc = 'known_hosts=None 只是文字'",
        "asyncssh.connect(h, known_hosts=pinned_known_hosts(x))",
    ])
    assert [n for n, _ in _none_known_hosts(sample)] == [1, 2]
