"""非 HTTP 的對外連線也要檢查目標位址（2026-10-09 合規核對：以前 SSRF 防護只涵蓋 HTTP）。

HTTP 走 core/safe_http（連線當下解析、檢查、連到檢查過的位址）。其餘協定（SSH、LDAP、SMTP、
RADIUS、WinRM、DNS、syslog、VNC／RDP 等主控台）在這裡檢查，規則依「誰決定目標」分兩種：

- **console**：主控台的目標是 IPAM 的 IP 記錄 —— 有子網路寫入權限的人可以自己建一筆 127.0.0.1
  或 169.254.169.254，再對它開主控台，把 jt-ipam 當跳板打本機服務或雲端中繼資料。所以擋：
  本機、未指定位址、link-local（含雲端中繼資料）、多播／廣播。私網、CGNAT 照常（那正是要連的）
- **integration**：管理員設定的整合目標。擋雲端中繼資料、link-local、多播／廣播、未指定位址；
  本機位址允許（本機的郵件轉送、LDAP、syslog 收集器都是常見設定）
- **strict**：跟 HTTP 完全相同的規則（safe_http.check_addrs：本機也擋、私網照 OUTBOUND_ALLOW_PRIVATE）。
  BIND 9、Windows DNS、Windows DHCP（WinRM）一直是這套，集中到這裡之後規則不變

兩種都先套管理員的允許清單（OUTBOUND_ALLOW_CIDRS）。以主機名稱設定的整合（例如要驗 TLS 憑證
名稱的 LDAP／SMTP）在連線前解析並檢查所有位址；函式庫連線時會再解析一次，所以這是連線前的
檢查，不是像 HTTP 那樣連到檢查過的位址。

守門：tests/test_net_guard.py（每個非 HTTP 的連線呼叫都要經過這裡，例外要寫理由）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Final, Literal

from app.core.safe_http import UnsafeOutboundURL, _canon, _ip_in, _parse_allow_cidrs

Policy = Literal["console", "integration", "strict"]
Addr = ipaddress.IPv4Address | ipaddress.IPv6Address

_METADATA: Final = (
    ipaddress.ip_network("169.254.0.0/16"),       # link-local（AWS／GCP／Azure 中繼資料 169.254.169.254）
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),     # AWS IPv6 中繼資料
    ipaddress.ip_network("100.100.100.200/32"),    # 阿里雲中繼資料
)
_NEVER: Final = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("255.255.255.255/32"),
)
_LOOPBACK: Final = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


class BlockedTarget(UnsafeOutboundURL):
    """被擋下的目標。`reason`：dns（解析不到）／private（私網沒允許）／blocked（本機、中繼資料、保留位址）。"""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def address_problem(addr: Addr, policy: Policy) -> str | None:
    """這個位址在這種用途下能不能連；能 → None，不能 → 原因代碼。"""
    from app.core.config import get_settings

    a = _canon(addr)
    allow = _parse_allow_cidrs(get_settings().outbound_allow_cidrs)
    if allow and _ip_in(a, allow):
        return None
    if _ip_in(a, _NEVER):
        return "reserved"
    if _ip_in(a, _METADATA):
        return "link_local"
    if policy == "console" and _ip_in(a, _LOOPBACK):
        return "loopback"
    return None


def _check(host: str, addrs: list[Addr], policy: Policy) -> None:
    if not addrs:
        raise BlockedTarget(f"No usable address for {host}", "dns")
    if policy == "strict":
        from app.core.safe_http import check_addrs
        try:
            check_addrs(host, addrs)
        except BlockedTarget:
            raise
        except UnsafeOutboundURL as exc:
            raise BlockedTarget(str(exc), "private" if str(exc).startswith("Private IP") else "blocked") from exc
        return
    for a in addrs:
        reason = address_problem(a, policy)
        if reason is not None:
            raise BlockedTarget(f"Blocked {policy} target {host} ({a}): {reason}", "blocked")


def _literal(host: str) -> Addr | None:
    try:
        return ipaddress.ip_address(host.strip("[]").split("%")[0])
    except ValueError:
        return None


def host_only(target: str) -> str:
    """設定值可能寫成 `ldaps://host:636`、`host:25`、`[2001:db8::1]:22` → 只取主機。"""
    t = (target or "").strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0]
    if t.startswith("["):
        return t[1:t.index("]")] if "]" in t else t.strip("[]")
    if t.count(":") == 1:
        t = t.split(":", 1)[0]
    return t


def check_target(host: str, port: int = 0, *, policy: Policy = "integration") -> list[Addr]:
    """（同步）解析並檢查；不安全丟 UnsafeOutboundURL。回傳檢查過的位址。"""
    host = host_only(host)
    lit = _literal(host)
    if lit is not None:
        addrs = [lit]
    else:
        try:
            infos = socket.getaddrinfo(host, port or None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise BlockedTarget(f"DNS resolution failed for {host}: {exc}", "dns") from exc
        addrs = []
        for *_x, sockaddr in infos:
            a = _literal(str(sockaddr[0]))
            if a is not None and a not in addrs:
                addrs.append(a)
    _check(host, addrs, policy)
    return addrs


async def acheck_target(host: str, port: int = 0, *, policy: Policy = "integration") -> list[Addr]:
    """（非同步）同 check_target，解析不卡事件迴圈。"""
    host = host_only(host)
    lit = _literal(host)
    if lit is not None:
        _check(host, [lit], policy)
        return [lit]
    return await asyncio.to_thread(check_target, host, port, policy=policy)
