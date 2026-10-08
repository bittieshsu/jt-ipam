"""Check Point 第二階段：閘道的 Gaia API（使用者 2026-10-08：要 DHCP 租約、ARP 表之類）。

Gaia API：`POST https://<閘道>/gaia_api/<指令>`，JSON；`login`（user/password）回 `sid`，之後帶
`X-chkp-sid`；**結束一定 logout**。指令與回應形狀照官方 Gaia API Reference 與官方 Ansible 模組
（check_point.gaia）；實機回應不同時以實機為準（tests/checkpoint_gaia_mock.py 一起改）。

讀什麼、要什麼權限：

- 唯讀（`sync_dhcp`，有 Gaia 帳密就做）：`show-dhcp-server` → 發放範圍（扣掉排除區間、略過停用的）
  寫進共用的 dhcp_pool_ranges，子網路設定另存鏡像（發給用戶端的閘道/DNS，改址評估用）
- 選用（`allow_scripts`，預設關）：Gaia API **沒有**讀 ARP 表或租約的指令（官方 Ansible 模組 114 支
  也沒有），只能用 `run-script`。那要能執行指令的帳號，所以由管理員明確打開，而且**只跑這裡寫死的
  指令**（`SCRIPTS`，以名稱選，任何外部輸入都不會進到指令裡）：
  - ARP：`ip -s neigh show`。`used a/b/c` 的第二個數字是「幾秒前確認過」→ 推回真正被確認的時間
    （跟 FortiOS 的 age 同一個道理），所以可以當上線證據；沒有計時資訊時只收 REACHABLE（定義上就是
    剛確認過，跟 MikroTik 同一個判斷）；PERMANENT／NOARP 不算、FAILED／INCOMPLETE 不收
  - 租約：讀 `/var/lib/dhcpd/dhcpd.leases`（Gaia 的 DHCP 伺服器是 ISC dhcpd）；檔案是日誌，同一個位址
    以最後一筆為準，只收 active 且沒到期的。檔案太大就不讀（先回報大小再決定）
  - VPN：`vpn tu tlist`／`cpstat vpn` 的輸出格式沒有穩定的機器可讀形狀，第二階段先不做

**不把閘道拖慢**：依序執行、每段之間喘息、輸出大小與等待時間都有上限。
**每段各自隔離**：某一段失敗不影響其他段；讀不到的那一段不清舊資料（沒看到≠刪掉）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safe_http import UnsafeOutboundURL, safe_request, transport_detail
from app.core.security import decrypt_secret, encrypt_secret
from app.core.sqlin import in_values
from app.core.ui_error import UiError

SOURCE = "checkpoint"
TIMEOUT = 30.0
MAX_BYTES = 64 * 1024 * 1024
PAUSE = 0.2                     # 每段之間喘息（秒）
SCRIPT_WAIT = 90.0              # run-script 最多等多久
SCRIPT_POLL = 1.0
MAX_LEASE_FILE = 32 * 1024 * 1024
MAX_ARP_ROWS = 200_000
MAX_LEASE_ROWS = 100_000

LEASE_FILE = "/var/lib/dhcpd/dhcpd.leases"

#: 會在閘道上執行的指令 —— **只有這幾個**，以名稱選。不可以用字串拼接或格式化插入任何值。
SCRIPTS: dict[str, str] = {
    "arp": "ip -s neigh show",
    # 先回報檔案大小，太大就不讀（避免把一個幾百 MB 的日誌整個搬過來）
    "leases": ("f=" + LEASE_FILE + "; s=$(wc -c 2>/dev/null < \"$f\") || s=-1; "
               "echo \"JTIPAM_SIZE $s\"; "
               "if [ \"$s\" -ge 0 ] && [ \"$s\" -le " + str(MAX_LEASE_FILE) + " ]; then cat \"$f\"; fi"),
    "probe": "echo jt-ipam",
}


class GaiaError(UiError):
    """呼叫 Gaia API 失敗。訊息只放指令、狀態與伺服器的錯誤碼／訊息，絕不放密碼或 sid。"""


def script_for(name: str) -> str:
    """以名稱取得寫死的指令；不在清單裡的一律 KeyError（守門：沒有任何路徑可以跑別的指令）。"""
    return SCRIPTS[name]


def _aad(target_id: Any) -> bytes:
    return f"checkpoint_gateway:{target_id}:gaia_secret".encode()


def encrypt_secret_for(target_id: Any, raw: str) -> tuple[bytes, bytes]:
    return encrypt_secret(raw, aad=_aad(target_id))


def _secret(t: Any) -> str:
    if not t.secret_enc or not t.secret_nonce:
        raise GaiaError("no Gaia password configured", code="cpg_no_secret")
    return decrypt_secret(t.secret_enc, t.secret_nonce, aad=_aad(t.id)).decode("utf-8")


def gaia_base(url: str) -> str:
    base = url.rstrip("/")
    return base if base.endswith("/gaia_api") else f"{base}/gaia_api"


def default_url(address: str | None) -> str | None:
    if not address:
        return None
    try:
        a = ipaddress.ip_address(str(address))
    except ValueError:
        return None
    host = f"[{a}]" if a.version == 6 else str(a)
    return f"https://{host}/gaia_api"


# ─────────────────── 連線 ───────────────────

class GaiaSession:
    """一次登入。`async with GaiaSession(target) as g:`，離開時一定登出。"""

    def __init__(self, target: Any, *, timeout: float = TIMEOUT) -> None:
        self.t, self.timeout = target, timeout
        self.base = gaia_base(target.gaia_url)
        self.sid: str | None = None
        self.version: str | None = None

    async def _post(self, cmd: str, body: dict[str, Any], *, login: bool = False) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.sid:
            headers["X-chkp-sid"] = self.sid
        try:
            resp = await safe_request("POST", f"{self.base}/{cmd}", content=json.dumps(body).encode(),
                                      headers=headers, timeout=self.timeout, verify=self.t.verify_tls,
                                      max_bytes=MAX_BYTES, follow_redirects=False)
        except UnsafeOutboundURL as exc:
            raise GaiaError(f"blocked by the outbound connection policy: {exc}", code="cpg_ssrf",
                            command=cmd, reason=str(exc)[:200]) from exc
        except httpx.HTTPError as exc:
            reason = transport_detail(exc)
            raise GaiaError(f"{cmd}: {reason}", code="cpg_transport", command=cmd, reason=reason) from exc
        if resp.is_redirect:
            loc = str(resp.headers.get("location") or "").split("?", 1)[0]
            raise GaiaError(f"{cmd}: redirected to {loc}", code="cpg_redirect", command=cmd, location=loc)
        try:
            data = resp.json()
        except ValueError as exc:
            raise GaiaError(f"{cmd}: HTTP {resp.status_code}, the reply is not JSON", code="cpg_not_json",
                            command=cmd, status=resp.status_code) from exc
        if resp.status_code != 200:
            err = str(data.get("code") or "") if isinstance(data, dict) else ""
            msg = str(data.get("message") or "")[:300] if isinstance(data, dict) else ""
            if login and (resp.status_code in (400, 401, 403) or "login" in err):
                raise GaiaError(f"login failed: {err or resp.status_code} {msg}".strip(), code="cpg_login_failed",
                                status=resp.status_code, reason=msg or err)
            if resp.status_code in (401, 403) or "permission" in err:
                raise GaiaError(f"{cmd}: {err or resp.status_code} {msg}".strip(), code="cpg_denied",
                                command=cmd, status=resp.status_code, reason=msg or err)
            if resp.status_code == 404:
                raise GaiaError(f"{cmd}: not supported by this Gaia API", code="cpg_unsupported", command=cmd,
                                status=resp.status_code, reason=msg or err)
            raise GaiaError(f"{cmd}: HTTP {resp.status_code} {err} {msg}".strip(), code="cpg_api_error",
                            command=cmd, status=resp.status_code, reason=(msg or err)[:300])
        if not isinstance(data, dict):
            raise GaiaError(f"{cmd}: unexpected reply", code="cpg_not_json", command=cmd, status=resp.status_code)
        return data

    async def __aenter__(self) -> GaiaSession:
        data = await self._post("login", {"user": self.t.username or "", "password": _secret(self.t)}, login=True)
        self.sid = str(data.get("sid") or "") or None
        if not self.sid:
            raise GaiaError("login: no session id in the reply", code="cpg_login_failed", reason="no sid")
        self.version = str(data.get("api-server-version") or "") or None
        if not self.version:
            try:
                v = await self.call("show-api-versions")
                self.version = str(v.get("current-version") or "") or None
            except GaiaError:
                self.version = None
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if not self.sid:
            return
        try:
            await self._post("logout", {})
        except GaiaError:
            pass                    # 登出失敗不蓋掉原本的錯誤；session 會自己逾時
        finally:
            self.sid = None

    async def call(self, cmd: str, **body: Any) -> dict[str, Any]:
        return await self._post(cmd, body)

    async def run_script(self, name: str, *, wait: float = SCRIPT_WAIT) -> str:
        """執行**寫死的**指令（以名稱選）並等它跑完，回傳輸出文字。"""
        script = script_for(name)
        started = await self._post("run-script", {"script": script, "description": f"jt-ipam read-only: {name}"})
        task_id = str(started.get("task-id") or "")
        details = started.get("task-details")
        if not task_id and not details:
            raise GaiaError(f"run-script {name}: no task id in the reply", code="cpg_api_error",
                            command="run-script", status=200, reason="no task-id")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        task: dict[str, Any] = {"status": "succeeded", "task-details": details} if details else {}
        while task_id:
            res = await self._post("show-task", {"task-id": task_id})
            tasks = res.get("tasks") or []
            task = tasks[0] if tasks and isinstance(tasks[0], dict) else {}
            status = str(task.get("status") or "").lower()
            if status and status not in ("in progress", "in-progress", "pending"):
                break
            if loop.time() >= deadline:
                raise GaiaError(f"run-script {name}: still running after {int(wait)} s", code="cpg_script_timeout",
                                script=name, seconds=int(wait))
            await asyncio.sleep(SCRIPT_POLL)
        detail = (task.get("task-details") or [{}])[0] if task.get("task-details") else {}
        status = str(task.get("status") or "").lower()
        rc = detail.get("return-value") if isinstance(detail, dict) else None
        out = _decode_output(detail.get("output") if isinstance(detail, dict) else None)
        if status == "failed" or (rc not in (None, 0)):
            err = _decode_output(detail.get("error") if isinstance(detail, dict) else None) or out
            raise GaiaError(f"run-script {name}: {status or 'failed'} (rc={rc}) {err[:200]}".strip(),
                            code="cpg_script_failed", script=name, reason=(err or status or "")[:300])
        return out


_B64 = re.compile(r"^[A-Za-z0-9+/=\r\n]+$")


def _decode_output(raw: Any) -> str:
    """run-script 的輸出：官方文件沒寫清楚是不是 base64（Management API 的是）—— 兩種都收。
    純文字輸出一定有空白或符號，不會整段都是 base64 字元。"""
    if raw is None:
        return ""
    s = str(raw)
    if s and _B64.match(s.strip()) and len(s.strip()) % 4 == 0:
        try:
            return base64.b64decode(s.strip(), validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return s
    return s


# ─────────────────── 解析 ───────────────────

def _ip4(v: Any) -> str | None:
    try:
        a = ipaddress.ip_address(str(v or "").strip())
    except ValueError:
        return None
    return str(a) if a.version == 4 else None


def _mac(v: Any) -> str | None:
    hexs = "".join(ch for ch in str(v or "").lower() if ch in "0123456789abcdef")
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2)) if len(hexs) == 12 else None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _subtract(ranges: list[tuple[int, int]], cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        cur = lo
        for s, e in sorted(cuts):
            if e < cur or s > hi:
                continue
            if s > cur:
                out.append((cur, s - 1))
            cur = max(cur, e + 1)
        if cur <= hi:
            out.append((cur, hi))
    return out


def parse_dhcp_server(data: dict[str, Any]) -> list[dict[str, Any]]:
    """`show-dhcp-server` → 每個子網路：CIDR、啟用、發放範圍（include 扣掉 exclude、略過停用的）、閘道、DNS。

    伺服器整體停用時每個子網路都算停用（設定還在，但沒有人在發位址）。"""
    server_on = bool(data.get("enabled", True))
    out: list[dict[str, Any]] = []
    for sn in data.get("subnets") or []:
        if not isinstance(sn, dict):
            continue
        addr = _ip4(sn.get("subnet"))
        mask = sn.get("netmask")
        try:
            cidr = str(ipaddress.ip_network(f"{addr}/{mask}", strict=False)) if addr and mask is not None else None
        except ValueError:
            cidr = None
        if not cidr:
            continue
        inc: list[tuple[int, int]] = []
        exc: list[tuple[int, int]] = []
        raw_pools: list[dict[str, Any]] = []
        for p in sn.get("ip-pools") or []:
            if not isinstance(p, dict):
                continue
            a, b = _ip4(p.get("start")), _ip4(p.get("end"))
            if not a or not b:
                continue
            on = bool(p.get("enabled", True))
            kind = str(p.get("include") or "include").lower()
            raw_pools.append({"start": a, "end": b, "include": kind, "enabled": on})
            if not on:
                continue
            pair = tuple(sorted((int(ipaddress.ip_address(a)), int(ipaddress.ip_address(b)))))
            (exc if kind == "exclude" else inc).append(pair)  # type: ignore[arg-type]
        dns = sn.get("dns") if isinstance(sn.get("dns"), dict) else {}
        out.append({
            "subnet": cidr, "enabled": server_on and bool(sn.get("enabled", True)),
            "pools": [(str(ipaddress.ip_address(x)), str(ipaddress.ip_address(y))) for x, y in _subtract(inc, exc)],
            "raw_pools": raw_pools,
            "default_gateway": _ip4(sn.get("default-gateway")),
            "dns_servers": [a for a in (_ip4(dns.get(k)) for k in ("primary", "secondary", "tertiary")) if a],
            "domain_name": str(dns.get("domain-name") or "").strip() or None,
            "default_lease": _int(sn.get("default-lease")), "max_lease": _int(sn.get("max-lease")),
        })
    return out


_NEIGH_SKIP = {"FAILED", "INCOMPLETE"}
_NEIGH_PERMANENT = {"PERMANENT", "NOARP"}


def parse_neigh(text: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """`ip -s neigh show` → [{ip, mac, state, seen_at, permanent}]。

    `used a/b/c`：a＝幾秒前用過、b＝幾秒前確認過、c＝幾秒前更新過。上線證據要的是「確認過」。"""
    now = now or datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        words = line.split()
        if len(words) < 2:
            continue
        try:
            ip = ipaddress.ip_address(words[0].split("%", 1)[0]).compressed
        except ValueError:
            continue
        upper = {w.upper() for w in words}
        if upper & _NEIGH_SKIP:
            continue
        mac = _mac(words[words.index("lladdr") + 1]) if "lladdr" in words and words.index("lladdr") + 1 < len(
            words) else None
        if mac is None:
            continue
        state = next((w.upper() for w in reversed(words) if w.isupper() and w.isalpha()), "")
        permanent = bool(upper & _NEIGH_PERMANENT)
        seen_at: datetime | None = None
        if "used" in words and words.index("used") + 1 < len(words):
            parts = words[words.index("used") + 1].split("/")
            confirmed = _int(parts[1]) if len(parts) >= 2 else None
            if confirmed is not None and 0 <= confirmed <= 86400:
                seen_at = now - timedelta(seconds=confirmed)
        elif state == "REACHABLE":
            seen_at = now
        out.append({"ip": ip, "mac": mac, "state": state, "seen_at": None if permanent else seen_at,
                    "permanent": permanent})
        if len(out) >= MAX_ARP_ROWS:
            break
    return out


_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|#[^\n]*|[{};,]|[^\s{};,"#]+')


def _tree(text: str) -> list[Any]:
    """dhcpd 租約檔的語法樹：[(字詞串, 子節點或 None)]。字串去掉引號、註解略過。"""
    root: list[Any] = []
    stack = [root]
    cur: list[str] = []
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if tok.startswith("#"):
            continue
        if tok == ";":
            if cur:
                stack[-1].append((cur, None))
            cur = []
        elif tok == "{":
            node: tuple[list[str], list[Any]] = (cur, [])
            stack[-1].append(node)
            stack.append(node[1])
            cur = []
        elif tok == "}":
            if cur:
                stack[-1].append((cur, None))
            cur = []
            if len(stack) > 1:
                stack.pop()
        elif tok.startswith('"'):
            cur.append(re.sub(r"\\(.)", r"\1", tok[1:-1]))
        else:
            cur.append(tok)
    return root


def _lease_time(words: list[str]) -> tuple[bool, datetime | None]:
    """`4 2026/09/24 13:02:03`（UTC）、`epoch 1695520923`、`never` → (永不到期, 時間)。"""
    if not words:
        return False, None
    if words[0].lower() == "never":
        return True, None
    try:
        if words[0].lower() == "epoch" and len(words) >= 2:
            return False, datetime.fromtimestamp(int(words[1]), tz=UTC)
        if len(words) >= 3:
            return False, datetime.strptime(f"{words[1]} {words[2]}", "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return False, None
    return False, None


def parse_leases(text: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """dhcpd.leases → 目前有效的租約（同一個位址以最後一筆為準；active 且沒到期或永不到期）。"""
    now = now or datetime.now(UTC)
    latest: dict[str, dict[str, Any]] = {}
    for words, kids in _tree(text):
        if kids is None or len(words) < 2 or words[0].lower() != "lease":
            continue
        ip = _ip4(words[1])
        if not ip:
            continue
        info: dict[str, Any] = {"state": None, "mac": None, "hostname": None, "never": False, "ends": None}
        for w, sub in kids:
            if sub is not None or not w:
                continue
            k = w[0].lower()
            if k == "binding" and len(w) >= 3 and w[1].lower() == "state":
                info["state"] = w[2].lower()
            elif k == "ends":
                info["never"], info["ends"] = _lease_time(w[1:])
            elif k == "hardware" and len(w) >= 3:
                info["mac"] = _mac(w[2])
            elif k == "client-hostname" and len(w) >= 2:
                info["hostname"] = w[1][:255]
        latest.pop(ip, None)
        latest[ip] = info
    out = []
    for ip, info in latest.items():
        if info["state"] != "active":
            continue
        if not info["never"] and (info["ends"] is None or info["ends"] <= now):
            continue
        out.append({"ip": ip, "mac": info["mac"], "hostname": info["hostname"],
                    "ends": info["ends"].isoformat() if info["ends"] else None})
        if len(out) >= MAX_LEASE_ROWS:
            break
    out.sort(key=lambda x: ipaddress.ip_address(x["ip"]))
    return out


def _split_size(output: str) -> tuple[int | None, str]:
    """leases 指令的第一行是 `JTIPAM_SIZE <位元組數>`（-1＝檔案不存在／讀不到）。"""
    head, _, rest = output.partition("\n")
    m = re.match(r"^JTIPAM_SIZE\s+(-?\d+)\s*$", head.strip())
    if not m:
        return None, output
    return int(m.group(1)), rest


# ─────────────────── 寫入 ───────────────────

def _scope_ids(t: Any, server: Any | None) -> list[Any] | None:
    ids = t.scope_subnet_ids or (server.scope_subnet_ids if server is not None else None)
    return list(ids) if ids else None


async def _write_dhcp(session: AsyncSession, t: Any, parsed: list[dict[str, Any]], now: datetime) -> int:
    from app.models.checkpoint_gaia import CheckPointDhcpSubnet
    from app.services.dhcp_standalone import write_pools

    live = [p for p in parsed if p["enabled"]]
    n = await write_pools(session, source_type="checkpoint", source_id=t.id,
                          source_name=t.name, engine=SOURCE,
                          pools=[{"subnet": p["subnet"], "start": a, "end": b} for p in live for a, b in p["pools"]])
    rows = {r.subnet_cidr: r for r in (await session.execute(select(CheckPointDhcpSubnet).where(
        CheckPointDhcpSubnet.target_id == t.id))).scalars().all()}
    seen = set()
    for p in parsed:
        seen.add(p["subnet"])
        row = rows.get(p["subnet"]) or CheckPointDhcpSubnet(target_id=t.id, subnet_cidr=p["subnet"])
        row.enabled = p["enabled"]
        row.default_gateway = p["default_gateway"]
        row.dns_servers = p["dns_servers"]
        row.domain_name = p["domain_name"]
        row.default_lease, row.max_lease = p["default_lease"], p["max_lease"]
        row.pools = p["raw_pools"]
        row.synced_at = now
        session.add(row)
    gone = [c for c in rows if c not in seen]
    if gone:
        await session.execute(delete(CheckPointDhcpSubnet).where(
            CheckPointDhcpSubnet.target_id == t.id, in_values(CheckPointDhcpSubnet.subnet_cidr, gone)))
    return n


async def _write_arp(session: AsyncSession, t: Any, rows: list[dict[str, Any]], scope: list[Any] | None) -> int:
    from app.services.fw_sightings import SightingBatch

    batch = SightingBatch(session, source=SOURCE, subnet_ids=scope)
    for r in rows:
        batch.add(r["ip"], evidence="arp:checkpoint" if r["seen_at"] or r["permanent"] else None,
                  mac=r["mac"], permanent=r["permanent"], seen_at=r["seen_at"])
    return sum(1 for found, _e in await batch.flush() if found)


async def _write_leases(session: AsyncSession, t: Any, leases: list[dict[str, Any]], scope: list[Any] | None) -> int:
    from app.models.checkpoint_gaia import CheckPointGaiaTarget
    from app.services.dhcp_leases import LeaseRun
    from app.services.fw_sightings import SightingBatch
    from app.services.hostname_reports import HostnameRun, enabled_peers

    hn_run = HostnameRun(session, source=SOURCE, origin=f"{SOURCE}:{t.id}",
                         peers=await enabled_peers(session, CheckPointGaiaTarget))
    lease_run = LeaseRun(session, source_type=SOURCE, source_id=t.id)
    batch = SightingBatch(session, source=SOURCE, subnet_ids=scope, lease_run=lease_run, hn_run=hn_run)
    for le in leases:
        name = le.get("hostname")
        batch.add(le["ip"], evidence="lease:checkpoint", mac=le.get("mac"),
                  hostname=name.split(".")[0] if name else None)
    seen = sum(1 for found, _e in await batch.flush() if found)
    await hn_run.finish(complete=True)
    await lease_run.finish(complete=True)
    return seen


# ─────────────────── 主流程 ───────────────────

async def _section(errors: dict[str, str], key: str, coro: Any) -> Any:
    """跑一個區段；失敗只記錯誤、回 None（其他區段照跑）。"""
    try:
        return await coro
    except GaiaError as exc:
        errors[key] = str(exc)[:300]
        return None


#: 讀不到但不算錯的原因（使用者 2026-10-08：「抓不到沒關係」）—— 記在摘要的 skipped，不寫 last_error、不告警
SKIP_NO_PERMISSION = "no_permission"     # 帳號不能執行指令（唯讀角色：HTTP 500 generic_err_no_permissions）
SKIP_UNSUPPORTED = "unsupported"         # 這版 Gaia API 沒有 run-script
SKIP_NO_LEASE_FILE = "no_lease_file"     # 閘道上沒有租約檔（版本或部署方式不同）
SKIP_DHCP_OFF = "dhcp_off"               # 閘道的 DHCP 伺服器沒有在發位址


def _skip_reason(exc: GaiaError) -> str | None:
    if exc.code == "cpg_denied":
        return SKIP_NO_PERMISSION
    if exc.code == "cpg_unsupported":
        return SKIP_UNSUPPORTED
    return None


async def sync_target(session: AsyncSession, t: Any) -> dict[str, Any]:
    """拉一次。登入失敗就往上拋（作業顯示失敗）；個別區段失敗記在摘要與 last_error。

    ARP 與租約要在閘道上執行指令：帳號沒權限、Gaia 沒有 run-script、閘道沒有租約檔、DHCP 伺服器沒開，
    都只記成略過（skipped），DHCP 設定照常同步，也不算同步失敗。"""
    from app.models.checkpoint import CheckPointServer

    now = datetime.now(UTC)
    server = await session.get(CheckPointServer, t.server_id)
    scope = _scope_ids(t, server)
    counts: dict[str, Any] = {}
    errors: dict[str, str] = {}
    skipped: dict[str, str] = {}
    dhcp_serving: bool | None = None        # None＝不知道（沒讀 DHCP 設定或讀失敗）
    try:
        async with GaiaSession(t) as g:
            if g.version and t.api_version != g.version:
                t.api_version = g.version
            if t.sync_dhcp:
                data = await _section(errors, "dhcp", g.call("show-dhcp-server"))
                if data is not None:
                    parsed = parse_dhcp_server(data)
                    counts["dhcp_subnets"] = len(parsed)
                    counts["pools"] = await _write_dhcp(session, t, parsed, now)
                    dhcp_serving = any(x["enabled"] for x in parsed)
                await asyncio.sleep(PAUSE)
            cannot_run: str | None = None       # 第一個指令就被拒的話，第二個不用再試

            async def script(key: str) -> str | None:
                nonlocal cannot_run
                if cannot_run:
                    skipped[key] = cannot_run
                    return None
                try:
                    return await g.run_script(key)
                except GaiaError as exc:
                    reason = _skip_reason(exc)
                    if reason is None:
                        errors[key] = str(exc)[:300]
                        return None
                    cannot_run = skipped[key] = reason
                    return None

            if t.allow_scripts and t.sync_arp:
                out = await script("arp")
                if out is not None:
                    rows = parse_neigh(out, now=datetime.now(UTC))
                    counts["arp_rows"] = len(rows)
                    counts["arp_matched"] = await _write_arp(session, t, rows, scope)
                await asyncio.sleep(PAUSE)
            if t.allow_scripts and t.sync_leases:
                if dhcp_serving is False:
                    # 沒有人在發位址：租約檔裡沒到期的舊紀錄也不是上線證據 → 當成「沒有租約」，清掉舊標記
                    skipped["leases"] = SKIP_DHCP_OFF
                    counts["leases"] = 0
                    counts["lease_matched"] = await _write_leases(session, t, [], scope)
                else:
                    out = await script("leases")
                    if out is not None:
                        size, body = _split_size(out)
                        if size is None or size < 0:
                            skipped["leases"] = SKIP_NO_LEASE_FILE      # 讀不到檔案不等於沒有租約：舊標記保留
                        elif size > MAX_LEASE_FILE:
                            errors["leases"] = f"{LEASE_FILE}: too large ({size} bytes, limit {MAX_LEASE_FILE})"
                        else:
                            leases = parse_leases(body, now=datetime.now(UTC))
                            counts["leases"] = len(leases)
                            counts["lease_matched"] = await _write_leases(session, t, leases, scope)
    except GaiaError as exc:
        t.last_error = str(exc)
        await session.commit()
        raise
    summary: dict[str, Any] = {**counts, "api_version": t.api_version}
    if skipped:
        summary["skipped"] = skipped
    if errors:
        summary["errors"] = errors
    t.last_summary = summary
    t.last_sync_at = datetime.now(UTC)
    t.last_error = ("部分區段失敗：" + "；".join(f"{k}: {v}" for k, v in errors.items())) if errors else None
    return summary


async def diagnose(t: Any) -> dict[str, Any]:
    """測試連線：版本、DHCP 設定讀不讀得到、可不可以執行指令（打開 allow_scripts 才試，跑 `echo`）。

    帳號沒權限或 Gaia 沒有 run-script 只回報在 scripts，不算錯誤（同步時那兩項會略過）。"""
    out: dict[str, Any] = {"version": None, "dhcp_subnets": None, "scripts": "not_enabled", "errors": {}}
    async with GaiaSession(t, timeout=20.0) as g:
        out["version"] = g.version
        try:
            out["dhcp_subnets"] = len(parse_dhcp_server(await g.call("show-dhcp-server")))
        except GaiaError as exc:
            out["errors"]["dhcp"] = str(exc)[:300]
        if t.allow_scripts:
            try:
                res = await g.run_script("probe", wait=30.0)
                out["scripts"] = "ok" if "jt-ipam" in res else "unexpected_output"
            except GaiaError as exc:
                reason = _skip_reason(exc)
                if reason == SKIP_NO_PERMISSION:
                    out["scripts"] = "denied"           # 同步時 ARP 與租約會略過，不算測試失敗
                elif reason == SKIP_UNSUPPORTED:
                    out["scripts"] = "unsupported"
                else:
                    out["scripts"] = "failed"
                    out["errors"]["scripts"] = str(exc)[:300]
    if not out["errors"]:
        out.pop("errors")
    return out


async def forget_target(session: AsyncSession, target_id: Any) -> None:
    """收回一台閘道寫進共用表的資料（發放範圍、租約旗標、主機名稱）。不 commit。"""
    from app.services.integration_cleanup import forget_instance
    await forget_instance(session, source=SOURCE, source_id=target_id)


async def forget_server_targets(session: AsyncSession, server_id: Any) -> int:
    """刪除管理伺服器時：它底下每台閘道寫進共用表的資料都要收回（鏡像表有外鍵 CASCADE）。"""
    from app.models.checkpoint_gaia import CheckPointGaiaTarget
    ids = list((await session.execute(select(CheckPointGaiaTarget.id).where(
        CheckPointGaiaTarget.server_id == server_id))).scalars().all())
    for tid in ids:
        await forget_target(session, tid)
    return len(ids)


__all__ = ["SCRIPTS", "GaiaError", "GaiaSession", "default_url", "diagnose", "encrypt_secret_for",
           "forget_server_targets", "forget_target", "parse_dhcp_server", "parse_leases", "parse_neigh",
           "script_for", "sync_target"]
