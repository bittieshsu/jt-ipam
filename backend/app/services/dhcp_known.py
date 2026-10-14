"""已知的 DHCP 伺服器：整合中的 DHCP 來源各自指向哪台主機。

「這台在發 DHCP 的是不是我們知道的伺服器」以前有兩份判斷：異常偵測「非授權 DHCP」認得十種整合，
IP 清單的紅色「非法 DHCP 伺服器」圖示只認 OPNsense／pfSense。客戶把 Windows DHCP 伺服器加進整合後，
異常偵測不報了、IP 清單還是紅的（2026-10-14）。兩份都漏了 MikroTik。現在異常偵測、IP 清單、
IP 調查都讀這裡；新增 DHCP 來源時在 COLLECTORS 加一筆（守門測試比對 DHCP_SOURCE_TYPES）。

人工標記（`ip_addresses.is_dhcp_server`）不在這裡：那是逐子網路的事實，由各呼叫端自己比對。
整合則可以跨網段成立（一台路由器服務好幾個網段，在別的網段被看到時不能變成消不掉的誤報）。

主機欄位可以是 IP 或名稱（Windows DHCP 走 WinRM 常填 FQDN）。名稱不在讀取路徑上查 DNS，
改用 IP 記錄的主機名稱對應：完整名稱相同，或其中一邊只寫短名稱時比第一段。
"""
from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: 畫面上的產品名稱（品牌名，不翻譯）
LABELS: dict[str, str] = {
    "opnsense": "OPNsense", "pfsense": "pfSense", "fortigate": "FortiGate", "paloalto": "Palo Alto",
    "mikrotik": "MikroTik", "kea_dhcp": "Kea", "isc_dhcp": "ISC DHCP", "windows_dhcp": "Windows DHCP",
    "isoinsight": "ISOinsight", "technitium": "Technitium", "checkpoint": "Check Point",
}


@dataclass(frozen=True)
class DhcpSource:
    kind: str                         # DHCP_SOURCE_TYPES 之一
    name: str                         # 整合名稱
    host: str                         # 正規化後的主機：IP 字串，或小寫、去掉結尾點的名稱
    ip: str | None                    # host 是 IP 時就是它
    scope: frozenset[str] | None      # 整合設定的子網路範圍（沒設＝None）

    @property
    def label(self) -> str:
        """產品名稱（IP 清單用：能看到那筆 IP 的人都看得到，不帶整合名稱）"""
        return LABELS.get(self.kind, self.kind)

    @property
    def display(self) -> str:
        """產品名稱＋整合名稱（需要全域讀取的畫面用，例如 IP 調查）"""
        return f"{self.label}「{self.name}」"


def _host_of(value: Any, *, url: bool) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    host = (urlparse(raw).hostname or "") if url and "://" in raw else raw
    host = host.strip().strip("[]").rstrip(".").lower()
    return host or None


def _norm_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.split("/")[0]))
    except ValueError:
        return None


Rows = list[tuple[str, Any, Any]]      # (整合名稱, 主機或網址, 範圍)


async def _url_rows(session: AsyncSession, model: Any, col: Any) -> Rows:
    scope = getattr(model, "scope_subnet_ids", None)
    cols = [model.name, col] + ([scope] if scope is not None else [])
    out: Rows = []
    for row in (await session.execute(select(*cols))).all():
        out.append((row[0], row[1], row[2] if scope is not None else None))
    return out


async def _opnsense(s: AsyncSession) -> Rows:
    from app.models.firewall import OPNsenseFirewall
    return await _url_rows(s, OPNsenseFirewall, OPNsenseFirewall.api_url)


async def _pfsense(s: AsyncSession) -> Rows:
    from app.models.pfsense import PfSenseFirewall
    return await _url_rows(s, PfSenseFirewall, PfSenseFirewall.api_url)


async def _fortigate(s: AsyncSession) -> Rows:
    from app.models.fortigate import FortiGateFirewall
    return await _url_rows(s, FortiGateFirewall, FortiGateFirewall.api_url)


async def _paloalto(s: AsyncSession) -> Rows:
    from app.models.paloalto import PaloAltoFirewall
    return await _url_rows(s, PaloAltoFirewall, PaloAltoFirewall.api_url)


async def _mikrotik(s: AsyncSession) -> Rows:
    from app.models.mikrotik import MikroTikRouter
    return await _url_rows(s, MikroTikRouter, MikroTikRouter.api_url)


async def _kea(s: AsyncSession) -> Rows:
    from app.models.dhcp_standalone import KeaDhcpServer
    return await _url_rows(s, KeaDhcpServer, KeaDhcpServer.api_url)


async def _isc(s: AsyncSession) -> Rows:
    # ISC DHCP 由裝在那台主機上的掃描代理讀設定檔：伺服器位址＝代理連進來的來源位址
    from app.models.dhcp_standalone import IscDhcpServer
    from app.models.scan_agent import ScanAgent
    rows = (await s.execute(select(IscDhcpServer.name, ScanAgent.last_source_ip, IscDhcpServer.scope_subnet_ids)
                            .join(ScanAgent, ScanAgent.id == IscDhcpServer.agent_id))).all()
    return [(n, str(ip) if ip else None, sc) for n, ip, sc in rows]


async def _windows(s: AsyncSession) -> Rows:
    from app.models.windows_dhcp import WindowsDhcpServer
    rows = (await s.execute(select(WindowsDhcpServer.name, WindowsDhcpServer.host,
                                   WindowsDhcpServer.scope_subnet_ids))).all()
    return [(n, h, sc) for n, h, sc in rows]


async def _isoinsight(s: AsyncSession) -> Rows:
    from app.models.isoinsight import IsoInsightSource
    return await _url_rows(s, IsoInsightSource, IsoInsightSource.base_url)


async def _technitium(s: AsyncSession) -> Rows:
    # 主控台網址的主機，加上它回報的 DHCP 介面位址（可以跟主控台不是同一個位址）
    from app.models.technitium import TechnitiumDhcpScope, TechnitiumDhcpServer
    out = await _url_rows(s, TechnitiumDhcpServer, TechnitiumDhcpServer.api_url)
    rows = (await s.execute(
        select(TechnitiumDhcpServer.name, TechnitiumDhcpScope.server_address, TechnitiumDhcpServer.scope_subnet_ids)
        .join(TechnitiumDhcpServer, TechnitiumDhcpServer.id == TechnitiumDhcpScope.server_id)
        .where(TechnitiumDhcpScope.server_address.is_not(None)).distinct())).all()
    return out + [(n, str(a), sc) for n, a, sc in rows]


async def _checkpoint(s: AsyncSession) -> Rows:
    # DHCP 由閘道發：管理伺服器同步下來的閘道位址，加上手動新增的 Gaia 目標
    from app.models.checkpoint import CheckPointGateway
    from app.models.checkpoint_gaia import CheckPointGaiaTarget
    out: Rows = [(n, str(ip), None) for n, ip in (await s.execute(
        select(CheckPointGateway.name, CheckPointGateway.ipv4_address)
        .where(CheckPointGateway.ipv4_address.is_not(None)))).all()]
    return out + await _url_rows(s, CheckPointGaiaTarget, CheckPointGaiaTarget.gaia_url)


#: 每一種 DHCP 來源怎麼找出它的主機。網址類的取主機部分，其餘是位址或名稱本身。
COLLECTORS: dict[str, tuple[Callable[[AsyncSession], Awaitable[Rows]], bool]] = {
    "opnsense": (_opnsense, True), "pfsense": (_pfsense, True), "fortigate": (_fortigate, True),
    "paloalto": (_paloalto, True), "mikrotik": (_mikrotik, True), "kea_dhcp": (_kea, True),
    "isc_dhcp": (_isc, False), "windows_dhcp": (_windows, False), "isoinsight": (_isoinsight, True),
    "technitium": (_technitium, True), "checkpoint": (_checkpoint, True),
}


@dataclass
class KnownDhcpServers:
    sources: list[DhcpSource]

    def __post_init__(self) -> None:
        self.by_ip: dict[str, list[DhcpSource]] = {}
        self.by_name: dict[str, list[DhcpSource]] = {}
        self.by_short: dict[str, list[DhcpSource]] = {}
        for src in self.sources:
            if src.ip:
                self.by_ip.setdefault(src.ip, []).append(src)
            else:
                self.by_name.setdefault(src.host, []).append(src)
                self.by_short.setdefault(src.host.split(".")[0], []).append(src)

    def match(self, ip: str | None, hostname: str | None = None, *, exact_names_only: bool = False
              ) -> list[DhcpSource]:
        """這個位址（＋它的 IP 記錄主機名稱）對得到哪些整合。

        `exact_names_only`：只認完整名稱相同（沒有「確實在發 DHCP」的觀測佐證時用，短名稱太容易撞名）。
        """
        out: list[DhcpSource] = []
        n_ip = _norm_ip(ip) if ip else None
        if n_ip:
            out += self.by_ip.get(n_ip, [])
        if hostname:
            h = str(hostname).strip().rstrip(".").lower()
            out += [s for s in self.by_name.get(h, []) if "." in h]
            if not exact_names_only:
                out += [s for s in self.by_name.get(h, []) if "." not in h]
                out += [s for s in self.by_short.get(h.split(".")[0], [])
                        if s.host != h and ("." not in s.host or "." not in h)]
        uniq: list[DhcpSource] = []
        for s in out:
            if s not in uniq:
                uniq.append(s)
        return uniq


async def load_known_dhcp_servers(session: AsyncSession) -> KnownDhcpServers:
    sources: list[DhcpSource] = []
    for kind, (collect, is_url) in COLLECTORS.items():
        for name, value, scope in await collect(session):
            host = _host_of(value, url=is_url)
            if not host:
                continue
            ip = _norm_ip(host)
            sources.append(DhcpSource(kind=kind, name=str(name), host=ip or host, ip=ip,
                                      scope=frozenset(str(x) for x in scope) if scope else None))
    return KnownDhcpServers(sources)
