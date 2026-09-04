"""MikroTik RouterOS 同步服務 —— REST（v7.1+），**全程唯讀（只打 GET）**。

這個整合與其他家最大的差別，是它的第一順位不是功能多寡，而是
**不可以把客戶的主力路由器拖慢**（CCR2004／CCR1072 是他們的邊界 router）。
設計上因此有四件事跟其他整合不一樣，全部寫在下面，動這個檔案前請先讀完：

1. **一輪只開一條連線**（`safe_client`）。`safe_request()` 每呼叫一次就新建一個
   client，等於每支端點各做一次 TLS 握手；CCR1072 是 Tile 架構（核多但單核弱，
   握手跑在單核上），十個區段就白費十次。
2. **序列執行 + 區段之間喘息**（`section_delay_ms`）。任何時刻只有一個請求在路上，
   **絕不平行打多支端點** —— 那是最容易讓 `www-ssl` 佔滿一顆核的作法。
   （注意這與 FortiGate 的 `diagnose()` 相反：那邊刻意並行以免十次逾時累加，
   這邊寧可診斷慢一點也不併發。）
3. **自我量測 + 自動退讓**。每個區段跑完重讀一次 `/system/resource` 的 `cpu-load`，
   超過門檻就停掉本輪剩下的區段並寫下原因（`last_cost.stopped`）。
4. **回應大小上限**（`max_response_mb`）。RouterOS 的 REST **沒有分頁也沒有 limit**，
   一支 `/ip/route` 在跑 BGP 的機器上可能是上百萬列 —— 讀完再判斷就已經 OOM 了。
   我們也因此**絕不抓** `/ip/route` 與 `/ip/firewall/connection`。

其他 RouterOS 特性：
- **所有值以字串回傳**（`"true"` / `"1500"`），一律轉型，不可直接當數字或布林用。
- 支援 `.proplist`（限定欄位，序列化才是路由器 CPU 的主要成本）與伺服器端過濾。
- v6 沒有 REST → 診斷要明講「這台是 RouterOS 6.x」，不要回含糊的連線失敗。
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import (
    ResponseTooLarge,
    UnsafeOutboundURL,
    safe_client,
    safe_request,
    transport_detail,
)
from app.core.security import decrypt_secret, encrypt_secret
from app.models.address import IPAddress
from app.models.mikrotik import MikroTikAddressList, MikroTikRouter, MikroTikRule
from app.services.hostname import apply_observation

# ── RouterOS 選單（REST 路徑＝CLI 路徑）──────────────────────────
EP_RESOURCE = "/system/resource"
EP_IDENTITY = "/system/identity"
EP_ROUTERBOARD = "/system/routerboard"
EP_ARP = "/ip/arp"
EP_LEASE = "/ip/dhcp-server/lease"
EP_DHCP_SERVER = "/ip/dhcp-server"
EP_DHCP_NETWORK = "/ip/dhcp-server/network"
EP_POOL = "/ip/pool"
EP_FILTER = "/ip/firewall/filter"
EP_MANGLE = "/ip/firewall/mangle"
EP_NAT = "/ip/firewall/nat"
EP_ADDRESS_LIST = "/ip/firewall/address-list"
EP_PPP_ACTIVE = "/ppp/active"
EP_WG_PEERS = "/interface/wireguard/peers"

#: ⛔ 永遠不要加進來的選單 —— 它們在主力路由器上是數十萬到數百萬列，
#: 而 REST 沒有分頁可以少拿一點。這不是效能微調，是安全上限。
NEVER_FETCH = ("/ip/route", "/ip/firewall/connection")

_DIAG_TIMEOUT = 10.0


class RouterOSError(Exception):
    """RouterOS 回了錯，或連不上。"""


class RouterOSNotPresent(RouterOSError):
    """這台裝置沒有這個選單（404）。

    與「錯誤」分開的理由：CRS309 交換器上沒有 `/ip/dhcp-server`、v7.0 沒有
    `/interface/wireguard` —— 那是**設備本來就沒有這個功能**，不是故障。
    混在一起的話，一台交換器同步完會掛著三行紅字，看起來像壞掉。
    """


# ─────────────────── 認證 ───────────────────
def _aad(router_id: uuid.UUID) -> bytes:
    return f"mikrotik_router:{router_id}:api_password".encode()


def encrypt_api_password(router_id: uuid.UUID, password: str) -> tuple[bytes, bytes]:
    return encrypt_secret(password, aad=_aad(router_id))


def _decrypt_password(router: MikroTikRouter) -> str:
    return decrypt_secret(
        router.api_password_enc, router.api_password_nonce, aad=_aad(router.id),
    ).decode("utf-8")


def _auth_header(router: MikroTikRouter) -> str:
    raw = f"{router.api_username}:{_decrypt_password(router)}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ─────────────────── 請求 ───────────────────
async def _get(
    router: MikroTikRouter,
    path: str,
    *,
    client: httpx.AsyncClient | None = None,
    proplist: str | None = None,
    filters: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> Any:
    """GET 一個 RouterOS 選單。

    `proplist` 限定回傳欄位、`filters` 做**伺服器端**過濾（例：`status=reachable`）——
    兩者都是為了讓路由器少序列化東西，不是為了我們少解析。
    """
    if path in NEVER_FETCH:      # 防呆：這條規則太重要，不能只寫在註解裡
        raise RouterOSError(f"{path} 屬於不可抓取的選單（列數可能是數十萬以上）")
    url = f"{router.api_url.rstrip('/')}/rest{path}"
    params: dict[str, Any] = dict(filters or {})
    if proplist:
        params[".proplist"] = proplist
    headers = {"Authorization": _auth_header(router), "Accept": "application/json"}
    max_bytes = max(1, int(router.max_response_mb or 8)) * 1024 * 1024
    try:
        resp = await safe_request(
            "GET", url, headers=headers, params=params or None, timeout=timeout,
            verify=router.verify_tls, client=client, max_bytes=max_bytes,
        )
    except UnsafeOutboundURL as exc:
        raise RouterOSError(f"SSRF guard rejected URL: {exc}") from exc
    except ResponseTooLarge as exc:
        raise RouterOSError(
            f"{path} 回應超過 {router.max_response_mb} MiB 已中止（{exc}）。"
            "RouterOS 的 REST 沒有分頁 —— 請關掉這個區段，或調高上限",
        ) from exc
    except httpx.HTTPError as exc:
        raise RouterOSError(f"transport: {transport_detail(exc)}") from exc

    if resp.status_code == 401:
        raise RouterOSError(
            "401 未授權：請確認帳號密碼正確，且該帳號所屬群組有 api + read 權限",
        )
    if resp.status_code == 403:
        raise RouterOSError("403 拒絕存取：帳號群組權限或 address 限制不足")
    if resp.status_code == 404:
        raise RouterOSNotPresent(f"這台裝置沒有 {path}（RouterOS 回 404）")
    if resp.status_code != 200:
        raise RouterOSError(f"GET {path}: {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        ctype = resp.headers.get("content-type", "?")
        snippet = " ".join(resp.text[:120].split())
        hint = ""
        if "html" in ctype.lower() or snippet.lower().startswith(("<!doctype", "<html")):
            hint = "（回的是網頁而非 API：這台可能是 RouterOS 6.x，REST 自 7.1 才有）"
        raise RouterOSError(
            f"回應不是 JSON（{path}）：{exc} content-type={ctype} 內容開頭={snippet!r}{hint}",
        ) from exc


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        return [data]
    return []


# ─────────────────── 型別轉換（RouterOS 全部回字串）───────────────────
def _b(v: object, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("true", "yes", "1")


def _num(v: object) -> float | None:
    if v is None:
        return None
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)", str(v))
    return float(m.group(1)) if m else None


def _txt(v: object, limit: int) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:limit] or None


_DUR = re.compile(r"(\d+)([wdhms])")
_DUR_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(v: object) -> float | None:
    """RouterOS 的時間長度：`4w2d3h`、`1m30s`、`00:01:30`、`never`。

    回秒數；解析不出來（含 `never`）回 None —— **回 None 不等於 0**，
    這個差別在 `last-seen` 上是「不知道什麼時候看到的」與「剛剛看到」之分。
    """
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s or s in ("never", "none"):
        return None
    if ":" in s and _DUR.search(s) is None:
        parts = s.split(":")
        if not all(p.replace(".", "", 1).isdigit() for p in parts) or len(parts) > 3:
            return None
        secs = 0.0
        for p in parts:
            secs = secs * 60 + float(p)
        return secs
    matches = _DUR.findall(s)
    if not matches:
        return None
    return float(sum(int(n) * _DUR_UNITS[u] for n, u in matches))


def _valid_ip(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip().split("/")[0]
    if not s:
        return None
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def _norm_mac(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower().replace("-", ":")
    parts = s.split(":")
    if len(parts) != 6 or not all(len(p) == 2 for p in parts):
        return None
    try:
        int(s.replace(":", ""), 16)
    except ValueError:
        return None
    return s


# ─────────────────── IP stamp（重疊網段安全）───────────────────
def _scope(router: MikroTikRouter) -> list[uuid.UUID] | None:
    return list(router.scope_subnet_ids) if router.scope_subnet_ids else None


async def _stamp_ip_seen(
    session: AsyncSession, ip: str, *, evidence: str,
    mac: str | None = None, hostname: str | None = None,
    subnet_ids: list[uuid.UUID] | None = None, dhcp: bool = False,
    seen_at: datetime | None = None,
) -> bool:
    """只標記「既有」IP，絕不新建（與其他防火牆整合一致）。"""
    ipx = _valid_ip(ip)
    if ipx is None:
        return False
    stmt = select(IPAddress).where(IPAddress.ip == ipx)
    if subnet_ids:
        stmt = stmt.where(IPAddress.subnet_id.in_(subnet_ids))
    ipa = (await session.execute(stmt.limit(1))).scalars().first()   # 重疊網段：取一筆
    if ipa is None:
        return False
    from app.services import arp_seen as arp_seen_svc
    arp_seen_svc.stamp(ipa, evidence, seen_at)
    if dhcp:
        ipa.in_dhcp_lease = True
    if mac:
        from app.services.arp_precedence import consider_mac
        await consider_mac(session, ip=ipa, mac=mac, source="mikrotik")
    if hostname:
        await apply_observation(session, ip=ipa, source="mikrotik", hostname=hostname)
    return True


# ─────────────────── system（同時是量測來源）───────────────────
async def read_resource(
    router: MikroTikRouter, *, client: httpx.AsyncClient | None = None,
    timeout: float = _DIAG_TIMEOUT,
) -> dict[str, Any]:
    """`/system/resource` —— 很輕，是退讓機制的量測點。"""
    rows = _rows(await _get(
        router, EP_RESOURCE, client=client, timeout=timeout,
        proplist="version,board-name,cpu-load,free-memory,total-memory,uptime",
    ))
    d = rows[0] if rows else {}
    return {
        "version": _txt(d.get("version"), 32),
        "board_name": _txt(d.get("board-name"), 64),
        "cpu_load": _num(d.get("cpu-load")),
        "free_memory": _num(d.get("free-memory")),
        "total_memory": _num(d.get("total-memory")),
        "uptime": _txt(d.get("uptime"), 32),
    }


def version_is_v6(version: str | None) -> bool:
    """v6 沒有 REST。第一版只支援 v7，而且要**明講**是版本問題。"""
    return bool(version) and str(version).strip().startswith("6.")


# ─────────────────── ARP（只收 reachable）───────────────────
#: RouterOS 7 的 `/ip/arp` **沒有 age／TTL／到期秒數**（唯讀屬性只有 complete /
#: dhcp / dynamic / invalid / status / VRF），所以無法像 OPNsense（expires）、
#: FortiOS（age）、PAN-OS（ttl）那樣換算「真正被看到的時刻」。
#: 判準改成只收 `reachable` —— 該狀態的定義就是「在可達性逾時（約 30 秒）內被確認過」，
#: 蓋上同步當下的時間才站得住。其餘狀態一律不記。
ARP_ACCEPTED_STATUS = ("reachable",)


async def sync_arp(
    session: AsyncSession, router: MikroTikRouter, *, client: httpx.AsyncClient,
) -> dict[str, Any]:
    rows = _rows(await _get(
        router, EP_ARP, client=client,
        proplist="address,mac-address,interface,status,complete,dynamic,dhcp",
        filters={"status": "reachable"},     # 伺服器端過濾：路由器少序列化，我們少收
    ))
    scope_ids = _scope(router)
    now = datetime.now(UTC)
    matched = 0
    for d in rows:
        # 即使已在伺服器端過濾，仍在本地再確認一次：舊版韌體可能忽略未知的查詢參數
        # 而回整張表 —— 那樣就會把 stale／permanent 當成上線證據。
        if str(d.get("status") or "").strip().lower() not in ARP_ACCEPTED_STATUS:
            continue
        ip = _valid_ip(d.get("address"))
        if not ip:
            continue
        if await _stamp_ip_seen(
            session, ip, evidence="arp:mikrotik",
            mac=_norm_mac(d.get("mac-address")), subnet_ids=scope_ids, seen_at=now,
        ):
            matched += 1
    return {"arp": matched, "arp_rows": len(rows)}


# ─────────────────── DHCP ───────────────────
async def sync_dhcp_leases(
    session: AsyncSession, router: MikroTikRouter, *, client: httpx.AsyncClient,
) -> dict[str, Any]:
    """`/ip/dhcp-server/lease` —— 只收 `bound`，並用 `last-seen` 推回真正被看到的時刻。"""
    rows = _rows(await _get(
        router, EP_LEASE, client=client,
        proplist="address,mac-address,host-name,status,last-seen,expires-after,"
                 "active-address,active-mac-address,dynamic,server",
        filters={"status": "bound"},
    ))
    scope_ids = _scope(router)
    now = datetime.now(UTC)
    leased: set[str] = set()
    seen = 0
    for d in rows:
        if str(d.get("status") or "").strip().lower() != "bound":
            continue    # 同 ARP：舊韌體可能忽略查詢參數
        ip = _valid_ip(d.get("active-address") or d.get("address"))
        if not ip:
            continue
        leased.add(ip)
        # `last-seen` 是「距離上次看到過了多久」→ 推回時刻；沒有就退回同步當下。
        # 租約本身歸 lease:mikrotik（不會過期、預設不採信為上線），所以這個時間
        # 只用來說明「這筆租約多新」，不會讓一台關機的機器顯示上線。
        ago = parse_duration(d.get("last-seen"))
        seen_at = now - timedelta(seconds=ago) if ago is not None else now
        if await _stamp_ip_seen(
            session, ip, evidence="lease:mikrotik",
            mac=_norm_mac(d.get("active-mac-address") or d.get("mac-address")),
            hostname=_txt(d.get("host-name"), 255),
            subnet_ids=scope_ids, dhcp=True, seen_at=seen_at,
        ):
            seen += 1
    # 撤銷：只在有設 scope 時做，避免多來源在全域互相清掉標記
    if scope_ids:
        stmt = sa_update(IPAddress).where(
            IPAddress.subnet_id.in_(scope_ids), IPAddress.in_dhcp_lease.is_(True))
        if leased:
            stmt = stmt.where(func.host(IPAddress.ip).notin_(leased))
        await session.execute(stmt.values(in_dhcp_lease=False))
    return {"dhcp": seen, "dhcp_rows": len(rows)}


def build_pool_ranges(
    pools: list[dict[str, Any]],
    servers: list[dict[str, Any]],
    networks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """三張表 join 成發放範圍。

    RouterOS 把一件事拆成三個選單，缺一個就組不出「哪個網段的哪一段由 DHCP 發放」：

    - `/ip/pool`：`{name, ranges: "10.0.0.100-10.0.0.200,10.0.0.240-10.0.0.250"}`
    - `/ip/dhcp-server`：`{name, interface, address-pool}` → pool 屬於哪台 DHCP 伺服器
    - `/ip/dhcp-server/network`：`{address: "10.0.0.0/24", gateway, dns-server, domain}`

    網段是**由範圍反查**出來的（server 本身只知道介面名稱）。純函式，方便單獨測。
    """
    by_name = {str(p.get("name")): p for p in pools if p.get("name")}
    nets: list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, dict[str, Any]]] = []
    for n in networks:
        try:
            nets.append((ipaddress.ip_network(str(n.get("address")), strict=False), n))
        except ValueError:
            continue

    out: list[dict[str, Any]] = []
    for srv in servers:
        if _b(srv.get("disabled")):
            continue
        pool = by_name.get(str(srv.get("address-pool") or ""))
        if pool is None:
            continue
        iface = _txt(srv.get("interface"), 64) or _txt(srv.get("name"), 64)
        for chunk in str(pool.get("ranges") or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            start, _, end = chunk.partition("-")
            a = _valid_ip(start)
            b = _valid_ip(end) or a          # 單一位址的 pool：起迄相同
            if not a or not b:
                continue
            net = next((cidr for cidr, _ in nets
                        if ipaddress.ip_address(a) in cidr), None)
            match = next((n for cidr, n in nets if str(cidr) == str(net)), {})
            out.append({
                "interface": iface,
                "subnet_cidr": str(net) if net else None,
                "start_ip": a,
                "end_ip": b,
                "family": 6 if ":" in a else 4,
                "gateway": _valid_ip(str(match.get("gateway") or "").split(",")[0]),
                "dns_servers": _txt(match.get("dns-server"), 255),
                "domain": _txt(match.get("domain"), 128),
            })
    return out


async def sync_dhcp_ranges(
    session: AsyncSession, router: MikroTikRouter, *, client: httpx.AsyncClient,
) -> dict[str, Any]:
    """發放範圍 → 共用的 `dhcp_pool_ranges`；順便補子網路空著的閘道／DNS。"""
    from app.models.dhcp import DHCPPoolRange
    from app.models.subnet import Subnet

    pools = _rows(await _get(router, EP_POOL, client=client, proplist="name,ranges"))
    await _breathe(router)
    servers = _rows(await _get(
        router, EP_DHCP_SERVER, client=client,
        proplist="name,interface,address-pool,disabled"))
    await _breathe(router)
    networks = _rows(await _get(
        router, EP_DHCP_NETWORK, client=client,
        proplist="address,gateway,dns-server,domain"))

    parsed = build_pool_ranges(pools, servers, networks)
    now = datetime.now(UTC)
    await session.execute(delete(DHCPPoolRange).where(
        DHCPPoolRange.source_type == "mikrotik", DHCPPoolRange.source_id == router.id))
    for r in parsed:
        session.add(DHCPPoolRange(
            source_type="mikrotik", source_id=router.id, source_name=router.name,
            subnet_cidr=r["subnet_cidr"] or r["interface"],
            start_ip=r["start_ip"], end_ip=r["end_ip"], family=r["family"],
            source="mikrotik", synced_at=now,
        ))

    # 只在欄位「原本是空的」時候填 —— 這是設備上的設定，但人工填的值優先。
    enriched = 0
    for r in parsed:
        if not r["subnet_cidr"] or not (r["gateway"] or r["dns_servers"]):
            continue
        subnet = (await session.execute(
            select(Subnet).where(Subnet.cidr == r["subnet_cidr"]).limit(1),
        )).scalars().first()
        if subnet is None:
            continue
        changed = False
        if r["gateway"] and not subnet.gateway:
            subnet.gateway = r["gateway"]
            changed = True
        if r["dns_servers"] and not subnet.dns_servers:
            subnet.dns_servers = r["dns_servers"]
            changed = True
        enriched += 1 if changed else 0
    return {"dhcp_ranges": len(parsed), "subnets_enriched": enriched}


# ─────────────────── 防火牆規則 ───────────────────
_RULE_PROPLIST = (
    "chain,action,disabled,comment,protocol,src-address,dst-address,"
    "src-port,dst-port,in-interface,out-interface,to-addresses,to-ports,"
    "src-address-list,dst-address-list"
)


def _rule_row(router_id: uuid.UUID, table: str, pos: int, d: dict[str, Any]) -> MikroTikRule:
    # address-list 也是一種來源／目的地：規則上寫的是清單名稱，不是位址。
    # 併進 src/dst 欄位（前綴標明）比另開兩欄好讀，也讓規則異動的比對涵蓋它。
    src = _txt(d.get("src-address"), 2048)
    if not src and d.get("src-address-list"):
        src = f"list:{_txt(d.get('src-address-list'), 128)}"
    dst = _txt(d.get("dst-address"), 2048)
    if not dst and d.get("dst-address-list"):
        dst = f"list:{_txt(d.get('dst-address-list'), 128)}"
    return MikroTikRule(
        router_id=router_id, table_name=table, position=pos,
        chain=_txt(d.get("chain"), 64), action=_txt(d.get("action"), 32),
        disabled=_b(d.get("disabled")),
        src_address=src, dst_address=dst,
        protocol=_txt(d.get("protocol"), 32),
        src_port=_txt(d.get("src-port"), 64), dst_port=_txt(d.get("dst-port"), 64),
        in_interface=_txt(d.get("in-interface"), 64),
        out_interface=_txt(d.get("out-interface"), 64),
        to_addresses=_txt(d.get("to-addresses"), 2048),
        to_ports=_txt(d.get("to-ports"), 64),
        comment=_txt(d.get("comment"), 2048),
        synced_at=datetime.now(UTC),
    )


async def sync_firewall_rules(
    session: AsyncSession, router: MikroTikRouter, *, client: httpx.AsyncClient,
) -> dict[str, Any]:
    """filter ＋ mangle → `mikrotik_rules`（鏡像取代）。

    ⚠️ **順序就是語意**：RouterOS 由上而下比對，第一條命中就決定結果。
    因此 `position` 用回傳順序寫入，規則異動偵測才看得出「規則被搬動了」。

    NAT 表不在這裡抓 —— `sync_nat()` 已經要讀它了，兩邊各抓一次等於白讓路由器
    多序列化一遍。它會自己把 `table_name="nat"` 的列寫進同一張表。
    """
    counts: dict[str, Any] = {}
    collected: list[MikroTikRule] = []
    for table, path in (("filter", EP_FILTER), ("mangle", EP_MANGLE)):
        try:
            rows = _rows(await _get(router, path, client=client, proplist=_RULE_PROPLIST))
        except RouterOSNotPresent:
            counts[f"{table}_rules"] = 0
            continue
        collected.extend(_rule_row(router.id, table, i, d) for i, d in enumerate(rows))
        counts[f"{table}_rules"] = len(rows)
        await _breathe(router)
    await session.execute(delete(MikroTikRule).where(
        MikroTikRule.router_id == router.id, MikroTikRule.table_name != "nat"))
    for row in collected:
        session.add(row)
    return counts


async def sync_address_lists(
    session: AsyncSession, router: MikroTikRouter, *, client: httpx.AsyncClient,
) -> dict[str, Any]:
    """`/ip/firewall/address-list` —— 等同其他家的 alias（鏡像取代）。

    ⚠️ 這是**本整合最可能爆量的一支**：拿來擋掃描的動態清單常常是上萬列。
    大小上限（`max_response_mb`）就是為了這種情況；超過時整個區段中止並回可讀錯誤。
    """
    rows = _rows(await _get(
        router, EP_ADDRESS_LIST, client=client,
        proplist="list,address,comment,dynamic,timeout"))
    now = datetime.now(UTC)
    await session.execute(
        delete(MikroTikAddressList).where(MikroTikAddressList.router_id == router.id))
    n = 0
    for d in rows:
        name = _txt(d.get("list"), 128)
        addr = _txt(d.get("address"), 2048)
        if not name or not addr:
            continue
        session.add(MikroTikAddressList(
            router_id=router.id, list_name=name, address=addr,
            dynamic=_b(d.get("dynamic")), timeout=_txt(d.get("timeout"), 32),
            comment=_txt(d.get("comment"), 2048), synced_at=now,
        ))
        n += 1
    return {"address_lists": n}


# ─────────────────── NAT（對外開口）───────────────────
def _first_port(v: object) -> int | None:
    """RouterOS 的埠可能是 `80`、`80,443` 或 `8000-8100` → 取第一個當代表值。

    存的是「這條規則開的埠」，用來讓曝險清單看得出 dst-nat 指向哪裡；完整字串仍在說明欄。
    """
    if v is None:
        return None
    s = str(v).strip().split(",")[0].split("-")[0].strip()
    if not s.isdigit():
        return None
    n = int(s)
    return n if 1 <= n <= 65535 else None


async def sync_nat(
    session: AsyncSession, router: MikroTikRouter, *, client: httpx.AsyncClient,
) -> dict[str, Any]:
    """`/ip/firewall/nat` → 共用的 `nat_translations`（只清自己的列）。

    型別要分三種，因為下游看的就是這一欄：
    - **帶目的埠的 dst-nat／netmap ＝ `port_forward`** —— 對外開放服務清單只認這個型別，
      標成 `one_to_one` 的話 MikroTik 的對外開口會整批從曝險檢視裡消失（最容易犯的錯）
    - 不帶埠的 dst-nat／netmap ＝ `one_to_one`（整台對應）
    - src-nat／masquerade ＝ `many_to_one`（出向偽裝，不是對外開口）
    """
    from app.models.nat import NATTranslation

    rows = _rows(await _get(router, EP_NAT, client=client, proplist=_RULE_PROPLIST))
    origin = f"mikrotik:{router.id}"
    await session.execute(
        delete(NATTranslation).where(NATTranslation.source_origin == origin))
    # 同一份資料兩種用途：規則鏡像（給規則異動偵測看順序與內容）與位址轉換（給 NAT 頁）
    await session.execute(delete(MikroTikRule).where(
        MikroTikRule.router_id == router.id, MikroTikRule.table_name == "nat"))
    for i, d in enumerate(rows):
        session.add(_rule_row(router.id, "nat", i, d))
    n = 0
    for i, d in enumerate(rows):
        if _b(d.get("disabled")):
            continue
        action = str(d.get("action") or "").strip().lower()
        dst_port = _first_port(d.get("dst-port"))
        to_port = _first_port(d.get("to-ports"))
        if action in ("dst-nat", "netmap"):
            kind = "port_forward" if (dst_port or to_port) else "one_to_one"
        elif action in ("src-nat", "masquerade"):
            kind = "many_to_one"
        else:
            continue        # accept / jump / log 等不是位址轉換
        proto = (_txt(d.get("protocol"), 8) or "any")[:8]
        label = _txt(d.get("comment"), 200) or f"{action} {_txt(d.get('chain'), 32) or ''}".strip()
        session.add(NATTranslation(
            name=label[:200], type=kind, protocol=proto,
            # 埠要存成欄位而不是只寫進說明：曝險清單是用 dst_port 判斷「不可判定」的
            dst_port=dst_port or to_port,
            src_interface=_txt(d.get("in-interface"), 64),
            description=" ".join(x for x in (
                f"{d.get('dst-address') or ''}:{d.get('dst-port') or ''}".strip(":"),
                f"→ {d.get('to-addresses') or ''}:{d.get('to-ports') or ''}".strip(":"),
            ) if x.strip(": →")) or None,
            source_origin=origin, external_id=f"{action}:{i}"[:200],
        ))
        n += 1
    return {"nat": n}


# ─────────────────── VPN ───────────────────
async def sync_vpn(
    session: AsyncSession, router: MikroTikRouter, *, client: httpx.AsyncClient,
) -> dict[str, Any]:
    """`/ppp/active`（撥入使用者）＋ `/interface/wireguard/peers`（站對站）。

    回傳除了計數，端點不存在時多一個 `*_absent` 旗標 —— `0` 有兩種完全不同的意思
    （「沒人連線」與「這台沒有這個功能」），從摘要看不出是哪一種就沒辦法判斷解析對不對。
    """
    from app.models.physical import VPNTunnel

    scope_ids = _scope(router)
    now = datetime.now(UTC)
    out: dict[str, Any] = {}

    try:
        ppp = _rows(await _get(
            router, EP_PPP_ACTIVE, client=client,
            proplist="name,service,address,caller-id,uptime"))
    except RouterOSNotPresent:
        ppp, out["ppp_absent"] = [], True
    stamped = 0
    for d in ppp:
        ip = _valid_ip(d.get("address"))
        if not ip:
            continue
        # 撥入中的連線＝此刻在線，時間就是現在（uptime 講的是連線多久，不是最後活動）
        if await _stamp_ip_seen(
            session, ip, evidence="vpn:mikrotik", subnet_ids=scope_ids, seen_at=now,
        ):
            stamped += 1
    out["vpn_sessions"] = stamped

    await _breathe(router)
    try:
        peers = _rows(await _get(
            router, EP_WG_PEERS, client=client,
            proplist="interface,name,public-key,endpoint-address,endpoint-port,"
                     "allowed-address,last-handshake,disabled"))
    except RouterOSNotPresent:
        peers, out["wireguard_absent"] = [], True

    prefix = f"{router.name}/wireguard/"
    seen_names: set[str] = set()
    for d in peers:
        label = _txt(d.get("name"), 64) or _txt(d.get("public-key"), 64)
        if not label:
            continue
        name = f"{prefix}{_txt(d.get('interface'), 32) or 'wg'}/{label}"[:128]
        seen_names.add(name)
        # 有握手時間才算通；WireGuard 沒有「連線」的概念，握手是唯一的活性訊號
        hs = parse_duration(d.get("last-handshake"))
        up = hs is not None and hs < 180 and not _b(d.get("disabled"))
        existing = (await session.execute(
            select(VPNTunnel).where(VPNTunnel.name == name).limit(1))).scalars().first()
        if existing is None:
            existing = VPNTunnel(name=name)
            session.add(existing)
        existing.type = "wireguard"
        existing.status = "active" if up else "down"
        existing.a_endpoint = _txt(router.api_url, 255)
        existing.b_endpoint = _txt(d.get("endpoint-address"), 255)
        existing.peer_public_key = _txt(d.get("public-key"), 255)
        existing.description = _txt(d.get("allowed-address"), 255)
    if seen_names or peers:
        await session.execute(delete(VPNTunnel).where(
            VPNTunnel.name.like(f"{prefix}%"), VPNTunnel.name.notin_(seen_names or {""})))
    out["vpn_tunnels"] = len(seen_names)
    return out


# ─────────────────── 節流 / 退讓 ───────────────────
async def _breathe(router: MikroTikRouter) -> None:
    """區段（與同一區段內的多支端點）之間喘一口氣。

    看起來像沒必要的睡眠，但這正是「不要把主 router 拖慢」最實際的一招：
    連續請求會讓 `www-ssl` 的序列化持續佔住一顆核。
    """
    delay = max(0, int(router.section_delay_ms or 0)) / 1000
    if delay:
        await asyncio.sleep(delay)


#: 由輕到重 —— 停在半路時，先做完的一定是最便宜、最常看的那幾段。
SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("firewall", "sync_firewall"),
    ("nat", "sync_nat"),
    ("dhcp_ranges", "sync_dhcp_ranges"),
    ("dhcp", "sync_dhcp"),
    ("vpn", "sync_vpn"),
    ("address_lists", "sync_address_lists"),
    ("arp", "sync_arp"),
)


# ─────────────────── 連線診斷 ───────────────────
async def diagnose(router: MikroTikRouter) -> dict[str, Any]:
    """測試連線：逐支端點回報**列數與耗時**，讓管理員自己決定要開哪些區段。

    ⚠️ 這裡刻意**序列**探測（其他整合的 diagnose 是並行的）。理由就是本檔案開頭第 2 點：
    對方是客戶的主力路由器，我們寧可診斷慢十秒，也不要同時開七條連線去打它。
    """
    out: dict[str, Any] = {"api_url": router.api_url}
    async with safe_client(timeout=_DIAG_TIMEOUT, verify=router.verify_tls) as client:
        try:
            info = await read_resource(router, client=client)
        except RouterOSError as exc:
            raise RouterOSError(f"無法讀取 /system/resource：{exc}") from exc
        out.update(info)
        if version_is_v6(info.get("version")):
            # 明講版本，不要讓現場對著「連線失敗」猜
            raise RouterOSError(
                f"這台是 RouterOS {info['version']}：6.x 沒有 REST API"
                "（REST 自 7.1beta4 起才有），本整合第一版只支援 v7",
            )
        try:
            ident = _rows(await _get(router, EP_IDENTITY, client=client, timeout=_DIAG_TIMEOUT))
            out["identity"] = _txt(ident[0].get("name"), 64) if ident else None
        except RouterOSError:
            out["identity"] = None

        probes = (
            ("dhcp_lease", EP_LEASE, {"status": "bound"}),
            ("dhcp_server", EP_DHCP_SERVER, None),
            ("dhcp_network", EP_DHCP_NETWORK, None),
            ("pool", EP_POOL, None),
            ("firewall_filter", EP_FILTER, None),
            ("firewall_nat", EP_NAT, None),
            ("address_list", EP_ADDRESS_LIST, None),
            ("arp", EP_ARP, {"status": "reachable"}),
            ("ppp_active", EP_PPP_ACTIVE, None),
            ("wireguard_peers", EP_WG_PEERS, None),
        )
        checks: list[dict[str, Any]] = []
        for label, path, filters in probes:
            t0 = time.monotonic()
            try:
                # 只取一個欄位：診斷要的是「幾列、多久」，不是內容。
                # 帶完整欄位去問一張上萬列的表，本身就是我們說好不做的事。
                rows = _rows(await _get(
                    router, path, client=client, proplist=".id",
                    filters=filters, timeout=_DIAG_TIMEOUT))
                checks.append({"endpoint": label, "ok": True, "rows": len(rows),
                               "seconds": round(time.monotonic() - t0, 2)})
            except RouterOSNotPresent:
                checks.append({"endpoint": label, "ok": True, "absent": True, "rows": 0,
                               "seconds": round(time.monotonic() - t0, 2)})
            except RouterOSError as exc:
                checks.append({"endpoint": label, "ok": False, "error": str(exc)[:200],
                               "seconds": round(time.monotonic() - t0, 2)})
            await _breathe(router)
        out["checks"] = checks
        out["ok_count"] = sum(1 for c in checks if c["ok"])
        try:
            after = await read_resource(router, client=client)
            out["cpu_load_after"] = after.get("cpu_load")
        except RouterOSError:
            pass
    return out


# ─────────────────── 整批同步 ───────────────────
async def sync_instance(session: AsyncSession, router: MikroTikRouter) -> dict[str, Any]:
    """跑此實例所有啟用的區段；設定 `last_sync_at` / `last_error` / `last_cost`。

    三層保護（缺一個都不算做到「不拖慢主 router」）：
    1. 序列 + 區段之間 `_breathe()`
    2. 每段跑完重讀 cpu-load，超過門檻就**停掉剩下的**（不是等下一輪才減量）
    3. 每段各自隔離：某段失敗（或這台沒有那個選單）不影響其他段
    """
    counts: dict[str, Any] = {}
    errors: dict[str, str] = {}
    cost: dict[str, Any] = {}
    done: set[str] = set()
    limit = int(router.cpu_load_limit or 0)

    async with safe_client(timeout=30.0, verify=router.verify_tls) as client:
        info = await read_resource(router, client=client)
        if version_is_v6(info.get("version")):
            raise RouterOSError(
                f"這台是 RouterOS {info['version']}：6.x 沒有 REST API，無法同步",
            )
        router.routeros_version = info.get("version")
        router.board_name = info.get("board_name")
        cost["cpu_before"] = info.get("cpu_load")

        handlers = {
            "firewall": (router.sync_firewall, sync_firewall_rules),
            "nat": (router.sync_nat, sync_nat),
            "dhcp_ranges": (router.sync_dhcp_ranges, sync_dhcp_ranges),
            "dhcp": (router.sync_dhcp, sync_dhcp_leases),
            "vpn": (router.sync_vpn, sync_vpn),
            "address_lists": (router.sync_address_lists, sync_address_lists),
            "arp": (router.sync_arp, sync_arp),
        }
        for name, _ in SECTION_ORDER:
            enabled, fn = handlers[name]
            if not enabled:
                continue
            t0 = time.monotonic()
            try:
                counts.update(await fn(session, router, client=client))
            except RouterOSNotPresent as exc:
                # 「這台沒有這個功能」不是錯誤（交換器沒有 DHCP 伺服器很正常）
                cost[name] = {"absent": True, "reason": str(exc)[:120]}
                continue
            except RouterOSError as exc:
                errors[name] = str(exc)[:200]
            else:
                done.add(name)
            cost[name] = {"seconds": round(time.monotonic() - t0, 2)}

            await _breathe(router)
            if limit > 0:
                try:
                    now_load = (await read_resource(router, client=client)).get("cpu_load")
                except RouterOSError:
                    now_load = None
                cost[name]["cpu_after"] = now_load
                if now_load is not None and now_load > limit:
                    # 停止而不是繼續放慢：主 router 忙的時候，最好的幫忙是走開。
                    cost["stopped"] = {
                        "after": name, "cpu_load": now_load, "limit": limit,
                        "reason": f"CPU {now_load:.0f}% 超過門檻 {limit}%，本輪剩下的區段略過",
                    }
                    counts["stopped_early"] = name
                    break

    # 規則異動偵測要等**所有寫 mikrotik_rules 的區段**都跑完才比對。
    # filter/mangle 與 nat 分屬兩個區段：只等前者的話，下一輪多出來的 nat 規則
    # 會整批被記成「新增」—— 一次假警報就足以讓人不再相信這一頁。
    # 同理，任何一段失敗就不比對：那會被記成「規則全部消失」（最嚇人的假警報）。
    expected = {n for n in ("firewall", "nat") if handlers[n][0]}
    if expected and expected <= done:
        from app.services.fw_review import run_sentinel
        await run_sentinel(session, source_type="mikrotik", instance=router)

    router.last_sync_at = datetime.now(UTC)
    router.last_cost = cost
    router.last_error = ("部分區段失敗：" + "；".join(f"{k}: {v}" for k, v in errors.items())
                         if errors else None)
    if errors:
        counts["errors"] = errors
    return counts
