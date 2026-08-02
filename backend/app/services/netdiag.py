"""網路診斷：ping / 路徑追蹤 / TCP 埠連通性。

與 `nettools.py`（純計算）分開：這裡會**實際送封包**，所以安全考量完全不同。

三個不可妥協的原則：

1. **絕不經過 shell。** 一律 `create_subprocess_exec` 加參數陣列 —— 目標字串永遠是
   一個獨立引數，不會被解讀成指令。目標另有嚴格驗證當第二道防線。
2. **一定有上限。** 目標數、併發數、單目標逾時、整體期限都設上限，避免一個請求
   就把伺服器或網路打爆（也避免被當成掃描跳板）。
3. **工具不存在要講清楚。** `traceroute` 在 Debian 預設不裝，這裡優先用 iputils 的
   `tracepath`（不需特權）；都沒有時回可行動的訊息，而不是一句「失敗」。

⚠️ ICMP 需要 `cap_net_raw`。Debian/Ubuntu 的 `ping` 預設帶 `cap_net_raw=ep`，所以
非 root 的服務帳號可以用；若某些環境把 capability 拔掉，ping 會失敗且訊息會指出這點。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

MAX_TARGETS = 64
MAX_CONCURRENCY = 32
MAX_COUNT = 10
MAX_HOPS = 30
OVERALL_DEADLINE = 120.0        # 單一請求的總時限，避免長時間佔住連線

# 主機名稱：字母/數字/連字號/點，且不以連字號起訖。這是第二道防線 ——
# 我們不經 shell，所以注入本來就不可能；但把明顯不是主機名稱的東西擋在外面仍然值得。
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)


class NetDiagError(ValueError):
    """輸入不合法或環境缺工具。"""


class NetDiagUnavailable(NetDiagError):
    """伺服器上沒有需要的工具 —— 端點回 501（不是使用者的錯，也不是逾時）。"""


def normalize_target(raw: str) -> str:
    """接受 IP 或主機名稱，其餘拒絕。回傳去空白後的字串。"""
    t = (raw or "").strip()
    if not t:
        raise NetDiagError("目標不可為空")
    if len(t) > 253:
        raise NetDiagError("目標過長")
    try:
        return str(ipaddress.ip_address(t))
    except ValueError:
        pass
    if _HOSTNAME_RE.match(t):
        return t
    raise NetDiagError(f"不是有效的 IP 或主機名稱：{raw}")


def expand_targets(raw: str | list[str]) -> list[str]:
    """把使用者輸入（換行/逗號/空白分隔，或 CIDR）展開成目標清單。

    支援 CIDR 是因為「這個網段裡哪些有回應」是最常見的用途；但要卡上限，
    不然一個 /16 就是六萬多個目標。
    """
    if isinstance(raw, list):
        parts = raw
    else:
        parts = [p for p in re.split(r"[\s,;]+", raw or "") if p]

    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if "/" in p:
            try:
                net = ipaddress.ip_network(p, strict=False)
            except ValueError as exc:
                raise NetDiagError(f"不是有效的網段：{p}") from exc
            hosts = list(net.hosts()) or [net.network_address]
            if len(out) + len(hosts) > MAX_TARGETS:
                raise NetDiagError(
                    f"{p} 展開後超過上限（最多 {MAX_TARGETS} 個目標）"
                )
            for h in hosts:
                s = str(h)
                if s not in seen:
                    seen.add(s)
                    out.append(s)
            continue
        t = normalize_target(p)
        if t not in seen:
            seen.add(t)
            out.append(t)
    if not out:
        raise NetDiagError("沒有可用的目標")
    if len(out) > MAX_TARGETS:
        raise NetDiagError(f"目標過多（{len(out)}），最多 {MAX_TARGETS} 個")
    return out


@dataclass
class PingResult:
    target: str
    alive: bool = False
    sent: int = 0
    received: int = 0
    loss_pct: float | None = None
    rtt_min_ms: float | None = None
    rtt_avg_ms: float | None = None
    rtt_max_ms: float | None = None
    error: str | None = None


# `ping -c N -W t` 的統計行，各發行版格式略有差異，只抓需要的數字
_STATS_RE = re.compile(r"(\d+) packets transmitted,\s*(\d+)\s*(?:packets )?received")
_RTT_RE = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))?\s*ms")


def parse_ping_output(target: str, out: str) -> PingResult:
    """解析 ping 輸出（純函式，好測；不同發行版的措辭差異都在這裡吸收）。"""
    r = PingResult(target=target)
    m = _STATS_RE.search(out)
    if m:
        r.sent, r.received = int(m.group(1)), int(m.group(2))
        r.alive = r.received > 0
        if r.sent:
            r.loss_pct = round((r.sent - r.received) / r.sent * 100, 1)
    m2 = _RTT_RE.search(out)
    if m2:
        r.rtt_min_ms = float(m2.group(1))
        r.rtt_avg_ms = float(m2.group(2))
        r.rtt_max_ms = float(m2.group(3))
    return r


async def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    """執行外部指令並取合併輸出。逾時就砍掉，不留孤兒程序。"""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, data.decode(errors="replace")


async def _run_partial(argv: list[str], timeout: float) -> tuple[str, bool]:
    """同上，但**逾時時回傳已經收到的輸出**而不是丟掉。

    路徑追蹤會逐跳印出結果，跑不完就整個放棄等於把已經查到的路徑丟掉 ——
    對診斷來說，「前 8 跳長這樣、後面沒跑完」遠比「逾時」有用。
    回 (輸出, 是否被截斷)。
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    chunks: list[bytes] = []
    truncated = False
    assert proc.stdout is not None
    try:
        async with asyncio.timeout(timeout):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                chunks.append(line)
    except TimeoutError:
        truncated = True
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return b"".join(chunks).decode(errors="replace"), truncated


def _ping_binary(target: str) -> str:
    """IPv6 目標在較舊的系統要用 ping6；新版 iputils 的 ping 兩者通吃。"""
    try:
        v6 = ipaddress.ip_address(target).version == 6
    except ValueError:
        v6 = False
    if v6 and shutil.which("ping") is None and shutil.which("ping6"):
        return "ping6"
    return "ping"


async def ping_many(
    targets: list[str], *, count: int = 3, timeout: float = 2.0, concurrency: int = 8,
) -> list[PingResult]:
    """對多個目標並行 ping。回傳順序與輸入一致（方便逐列對照）。"""
    if shutil.which("ping") is None and shutil.which("ping6") is None:
        raise NetDiagUnavailable("伺服器上找不到 ping（請安裝 iputils-ping）")
    count = max(1, min(count, MAX_COUNT))
    concurrency = max(1, min(concurrency, MAX_CONCURRENCY))
    timeout = max(0.5, min(timeout, 10.0))
    # 單目標最壞情況約 count × timeout，再加點餘裕；另有整體期限兜底
    per_target = count * timeout + 5.0
    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    async def one(t: str) -> PingResult:
        async with sem:
            if time.monotonic() - started > OVERALL_DEADLINE:
                return PingResult(target=t, error="整體時間上限已到，未執行")
            argv = [_ping_binary(t), "-n", "-c", str(count), "-W", str(int(max(1, timeout))), t]
            try:
                _, out = await _run(argv, per_target)
            except TimeoutError:
                return PingResult(target=t, sent=count, received=0, loss_pct=100.0,
                                  error="逾時")
            except OSError as exc:
                return PingResult(target=t, error=f"無法執行 ping：{exc}")
            res = parse_ping_output(t, out)
            if not res.sent and not res.alive:
                # 沒有統計行通常是名稱解析失敗一類；把第一行原文帶回去比「失敗」有用
                first = next((ln for ln in out.splitlines() if ln.strip()), "")
                res.error = first[:200] or "沒有回應"
            return res

    return list(await asyncio.gather(*(one(t) for t in targets)))


@dataclass
class TraceHop:
    hop: int
    host: str | None = None
    rtt_ms: float | None = None
    note: str | None = None


@dataclass
class TraceResult:
    target: str
    tool: str
    hops: list[TraceHop] = field(default_factory=list)
    path_mtu: int | None = None
    truncated: bool = False      # 逾時被截斷：下面的躍點是已探到的部分，不是完整路徑
    raw: str = ""


# tracepath 每一跳可能印多行（每次探測一行），沒回應的那跳印 "no reply"。
_TRACEPATH_RE = re.compile(r"^\s*(\d+):\s+(no reply|\S+)\s*(?:([\d.]+)ms)?(.*)$")
_TRACEROUTE_RE = re.compile(r"^\s*(\d+)\s+(.*)$")
_PMTU_RE = re.compile(r"pmtu\s+(\d+)")


def parse_tracepath(target: str, out: str) -> TraceResult:
    """解析 tracepath 輸出（含路徑 MTU —— traceroute 沒有這個資訊）。"""
    res = TraceResult(target=target, tool="tracepath", raw=out)
    seen: set[int] = set()
    for line in out.splitlines():
        m = _PMTU_RE.search(line)
        if m and res.path_mtu is None:
            res.path_mtu = int(m.group(1))
        m2 = _TRACEPATH_RE.match(line)
        if not m2:
            continue
        hop = int(m2.group(1))
        host = m2.group(2)
        if host == "[LOCALHOST]" or hop in seen:
            continue          # 同一跳的重複探測只留第一筆
        seen.add(hop)
        no_reply = host == "no reply"
        # 沒回應的那一跳仍然要列出來。把它省略會讓路徑看起來 1→3→5 中間沒東西，
        # 讀的人會以為那幾跳不存在，而不是「不回應」——那是誤導。
        res.hops.append(TraceHop(
            hop=hop,
            host=None if no_reply else host,
            rtt_ms=float(m2.group(3)) if m2.group(3) else None,
            note="無回應" if no_reply else ((m2.group(4) or "").strip() or None),
        ))
    return res


def parse_traceroute(target: str, out: str) -> TraceResult:
    res = TraceResult(target=target, tool="traceroute", raw=out)
    for line in out.splitlines():
        m = _TRACEROUTE_RE.match(line)
        if not m or line.lstrip().startswith("traceroute"):
            continue
        rest = m.group(2).strip()
        rtt = re.search(r"([\d.]+)\s*ms", rest)
        host = None if rest.startswith("*") else rest.split()[0]
        res.hops.append(TraceHop(
            hop=int(m.group(1)), host=host,
            rtt_ms=float(rtt.group(1)) if rtt else None,
            note=None if host else "無回應",
        ))
    return res


async def traceroute(target: str, *, max_hops: int = 20, timeout: float = 0.0) -> TraceResult:
    """路徑追蹤。

    優先用 `tracepath`：Debian/Ubuntu 的 iputils 內建、**不需特權**，而且會回報
    路徑 MTU（診斷 MTU 黑洞很有用）。沒有才退回 `traceroute`。
    """
    max_hops = max(1, min(max_hops, MAX_HOPS))
    # 逾時要跟著躍點數走：對不回 port-unreachable 的目標（如 8.8.8.8），tracepath
    # 會一路探到最大躍點，每跳都要等。固定 45 秒配預設 20 跳＝開箱必定逾時。
    timeout = max(5.0, min(timeout if timeout > 0 else 6.0 + max_hops * 3.5, OVERALL_DEADLINE))
    if shutil.which("tracepath"):
        argv = ["tracepath", "-n", "-m", str(max_hops), target]
        parse = parse_tracepath
    elif shutil.which("traceroute"):
        argv = ["traceroute", "-n", "-m", str(max_hops), "-w", "2", target]
        parse = parse_traceroute
    else:
        raise NetDiagUnavailable(
            "伺服器上找不到 tracepath 或 traceroute（請安裝 iputils-tracepath）"
        )
    try:
        out, truncated = await _run_partial(argv, timeout)
    except OSError as exc:
        raise NetDiagError(f"無法執行：{exc}") from exc
    res = parse(target, out)
    res.truncated = truncated
    return res


@dataclass
class PortResult:
    target: str
    port: int
    open: bool = False
    latency_ms: float | None = None
    error: str | None = None


async def tcp_check(
    targets: list[str], ports: list[int], *, timeout: float = 2.0, concurrency: int = 16,
) -> list[PortResult]:
    """TCP 連線測試。

    純 asyncio，不需外部指令也不需特權 —— 而且對**擋 ICMP 的主機**比 ping 有用：
    ping 不通不代表服務不通，這裡測的是實際要用的那個埠。
    """
    concurrency = max(1, min(concurrency, MAX_CONCURRENCY))
    timeout = max(0.2, min(timeout, 10.0))
    for p in ports:
        if not 1 <= p <= 65535:
            raise NetDiagError(f"埠號超出範圍：{p}")
    if len(targets) * len(ports) > MAX_TARGETS * 4:
        raise NetDiagError("目標與埠的組合過多")

    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    async def one(t: str, port: int) -> PortResult:
        async with sem:
            if time.monotonic() - started > OVERALL_DEADLINE:
                return PortResult(target=t, port=port, error="整體時間上限已到，未執行")
            t0 = time.monotonic()
            writer = None
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(t, port), timeout=timeout)
                return PortResult(target=t, port=port, open=True,
                                  latency_ms=round((time.monotonic() - t0) * 1000, 2))
            except TimeoutError:
                return PortResult(target=t, port=port, error="逾時")
            except OSError as exc:
                return PortResult(target=t, port=port, error=exc.strerror or str(exc))
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

    jobs = [one(t, p) for t in targets for p in ports]
    return list(await asyncio.gather(*jobs))


def tool_availability() -> dict[str, Any]:
    """哪些診斷工具在這台機器上可用 —— 前端據此把不能用的功能標示出來。"""
    return {
        "ping": bool(shutil.which("ping") or shutil.which("ping6")),
        "tracepath": bool(shutil.which("tracepath")),
        "traceroute": bool(shutil.which("traceroute")),
        "tcp": True,   # 純 Python，永遠可用
    }


# ─────────────────── UDP 探測 ───────────────────

@dataclass
class UdpResult:
    target: str
    port: int
    state: str = "no_reply"     # open / closed / no_reply
    probe: str = "empty"        # 送出去的是哪種封包
    latency_ms: float | None = None
    reply_bytes: int | None = None
    detail: str | None = None


def _dns_query_packet() -> bytes:
    """對根區送一個 NS 查詢。任何 DNS 伺服器都會回應，所以「有回應」＝確定開著。"""
    return (b"\x12\x34"            # transaction id
            b"\x01\x00"            # standard query, recursion desired
            b"\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x00"                # root
            b"\x00\x02\x00\x01")   # NS, IN


def _ntp_packet() -> bytes:
    """LI=0 VN=3 Mode=3（client），其餘留空 —— 48 bytes。"""
    return b"\x1b" + b"\x00" * 47


# 對這些埠送真實協定封包，才拿得到「確定開啟」而不是一片沉默。
_UDP_PROBES: dict[int, tuple[str, bytes]] = {
    53: ("dns", _dns_query_packet()),
    123: ("ntp", _ntp_packet()),
}


class _UdpProto(asyncio.DatagramProtocol):
    def __init__(self, done: asyncio.Future) -> None:
        self.done = done

    def datagram_received(self, data: bytes, addr: object) -> None:
        if not self.done.done():
            self.done.set_result(("open", data))

    def error_received(self, exc: Exception) -> None:
        # ICMP port unreachable 會以 ConnectionRefusedError 回到這裡 ——
        # 這是 UDP 唯一能「確定關閉」的訊號，不需要 raw socket。
        if not self.done.done():
            self.done.set_result(("closed", b""))


async def udp_check(
    targets: list[str], ports: list[int], *, timeout: float = 2.0, concurrency: int = 16,
) -> list[UdpResult]:
    """UDP 埠探測。

    ⚠️ **UDP 沒有交握，「沒有回應」本質上無法判定** —— 可能開著但不回應、被防火牆
    丟棄、或封包遺失。所以這裡回三種狀態而不是二元的開/關：

      open     收到回應（或 ICMP 以外的資料）→ 確定開著
      closed   收到 ICMP port unreachable    → 確定關閉
      no_reply 什麼都沒收到                   → **無法判定**，交給使用者判斷

    把 no_reply 顯示成「開啟」會是安靜地說謊，所以它自成一態。
    對 53 / 123 會送真實協定封包，否則多數服務根本不會回應空封包。
    """
    concurrency = max(1, min(concurrency, MAX_CONCURRENCY))
    timeout = max(0.2, min(timeout, 10.0))
    for p in ports:
        if not 1 <= p <= 65535:
            raise NetDiagError(f"埠號超出範圍：{p}")
    if len(targets) * len(ports) > MAX_TARGETS * 4:
        raise NetDiagError("目標與埠的組合過多")

    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    async def one(host: str, port: int) -> UdpResult:
        async with sem:
            if time.monotonic() - started > OVERALL_DEADLINE:
                return UdpResult(target=host, port=port, detail="整體時間上限已到，未執行")
            name, payload = _UDP_PROBES.get(port, ("empty", b"\x00"))
            res = UdpResult(target=host, port=port, probe=name)
            loop = asyncio.get_running_loop()
            done: asyncio.Future = loop.create_future()
            transport = None
            t0 = time.monotonic()
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _UdpProto(done), remote_addr=(host, port))
                transport.sendto(payload)
                state, data = await asyncio.wait_for(done, timeout=timeout)
                res.state = state
                res.latency_ms = round((time.monotonic() - t0) * 1000, 2)
                if state == "open":
                    res.reply_bytes = len(data)
                    res.detail = _describe_reply(name, data)
            except TimeoutError:
                res.state = "no_reply"
            except OSError as exc:
                res.state = "closed" if isinstance(exc, ConnectionRefusedError) else "no_reply"
                res.detail = exc.strerror or str(exc)
            finally:
                if transport is not None:
                    transport.close()
            return res

    jobs = [one(t, p) for t in targets for p in ports]
    return list(await asyncio.gather(*jobs))


def _describe_reply(probe: str, data: bytes) -> str | None:
    """對已知協定，把回應解讀成一句人看得懂的話。"""
    if probe == "dns" and len(data) >= 4:
        rcode = data[3] & 0x0F
        names = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 5: "REFUSED"}
        return f"DNS {names.get(rcode, f'rcode={rcode}')}"
    if probe == "ntp" and len(data) >= 48:
        stratum = data[1]
        return f"NTP stratum {stratum}" if stratum else "NTP (kiss-o'-death / 未同步)"
    return None
