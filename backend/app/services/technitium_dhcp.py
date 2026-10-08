"""Technitium DNS Server 的 DHCP：範圍、保留、租約（使用者 2026-10-08）。

API（實機 15.6 確認，token 放 POST 表單，見 services/technitium.py）：
  /api/dhcp/scopes/list           範圍摘要（name、enabled、起訖、子網路遮罩）
  /api/dhcp/scopes/get name=<n>   單一範圍：保留（reservedLeases）、選項（閘道／DNS／NTP／WINS）、排除區間
  /api/dhcp/leases/list           全部租約（含保留的；leaseExpires 是 UTC）

寫法比照 Kea（services/kea_dhcp.py）：發放範圍、保留、租約進共用的寫入層（services/dhcp_standalone.py），
source_type = "technitium"；另存範圍鏡像（technitium_dhcp_scopes）給改址評估查「這個位址是哪個範圍發出去的閘道／DNS」。

- 停用的範圍不發位址：不寫發放範圍、保留與選項；鏡像照留、標停用
- 排除區間從發放範圍挖掉，「IP 在發放範圍內」才準
- 讀不到就整次失敗、寫 last_error，不清任何東西（沒看到≠刪掉）
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.core.sqlin import in_values
from app.services.technitium import TechnitiumClient, TechnitiumError

SOURCE = "technitium"
MAX_SCOPES = 2000          # 逐個範圍讀保留與選項的上限（超過就停，摘要標出來）


def _aad(instance_id: Any) -> bytes:
    return f"technitium_dhcp_server:{instance_id}:token".encode()


def encrypt_token(instance_id: Any, raw: str) -> tuple[bytes, bytes]:
    return encrypt_secret(raw, aad=_aad(instance_id))


def _token(inst: Any) -> str:
    if not inst.token_enc or not inst.token_nonce:
        raise TechnitiumError("no API token configured", code="technitium_no_token")
    return decrypt_secret(inst.token_enc, inst.token_nonce, aad=_aad(inst.id)).decode("utf-8")


def client_for(inst: Any) -> TechnitiumClient:
    return TechnitiumClient(api_url=inst.api_url, token=_token(inst), verify_tls=inst.verify_tls)


def _mac(v: Any) -> str | None:
    hexs = "".join(ch for ch in str(v or "").lower() if ch in "0123456789abcdef")
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2)) if len(hexs) == 12 else None


def _ip4(v: Any) -> str | None:
    try:
        a = ipaddress.ip_address(str(v or "").strip())
    except ValueError:
        return None
    return str(a) if a.version == 4 else None


def _ips(v: Any) -> list[str]:
    return [a for a in (_ip4(x) for x in (v or []) if x) if a] if isinstance(v, list) else []


def pool_ranges(start: str, end: str, exclusions: Any) -> list[tuple[str, str]]:
    """發放範圍扣掉排除區間後剩下的幾段（壞掉的排除項略過，不讓整個範圍消失）。"""
    a, b = _ip4(start), _ip4(end)
    if not a or not b:
        return []
    lo, hi = sorted((int(ipaddress.ip_address(a)), int(ipaddress.ip_address(b))))
    cuts: list[tuple[int, int]] = []
    for ex in exclusions or []:
        if not isinstance(ex, dict):
            continue
        s, e = _ip4(ex.get("startingAddress")), _ip4(ex.get("endingAddress"))
        if s and e:
            cuts.append(tuple(sorted((int(ipaddress.ip_address(s)), int(ipaddress.ip_address(e))))))  # type: ignore[arg-type]
    out: list[tuple[str, str]] = []
    cur = lo
    for s, e in sorted(cuts):
        if e < cur or s > hi:
            continue
        if s > cur:
            out.append((cur, s - 1))  # type: ignore[arg-type]
        cur = max(cur, e + 1)
    if cur <= hi:
        out.append((cur, hi))  # type: ignore[arg-type]
    return [(str(ipaddress.ip_address(x)), str(ipaddress.ip_address(y))) for x, y in out]


def parse_scope(sc: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    start, end = _ip4(sc.get("startingAddress")), _ip4(sc.get("endingAddress"))
    subnet = None
    try:
        if start and sc.get("subnetMask"):
            subnet = str(ipaddress.ip_network(f"{start}/{sc['subnetMask']}", strict=False))
    except ValueError:
        subnet = None
    try:
        lease = (int(sc.get("leaseTimeDays") or 0) * 86400 + int(sc.get("leaseTimeHours") or 0) * 3600
                 + int(sc.get("leaseTimeMinutes") or 0) * 60) or None
    except (TypeError, ValueError):
        lease = None
    reservations = []
    for r in sc.get("reservedLeases") or []:
        ip = _ip4(r.get("address")) if isinstance(r, dict) else None
        if ip:
            reservations.append({"ip": ip, "mac": _mac(r.get("hardwareAddress")),
                                 "hostname": r.get("hostName") or None, "subnet": subnet})
    excl = [{"start": s, "end": e} for s, e in
            ((_ip4(x.get("startingAddress")), _ip4(x.get("endingAddress")))
             for x in (sc.get("exclusions") or []) if isinstance(x, dict)) if s and e]
    return {"name": str(sc.get("name") or ""), "enabled": enabled, "subnet": subnet, "start": start, "end": end,
            "exclusions": excl, "pools": pool_ranges(start or "", end or "", sc.get("exclusions")),
            "router": _ip4(sc.get("routerAddress")), "dns_servers": _ips(sc.get("dnsServers")),
            "ntp_servers": _ips(sc.get("ntpServers")), "wins_servers": _ips(sc.get("winsServers")),
            "domain_name": sc.get("domainName") or None, "lease_seconds": lease, "reservations": reservations}


def _when(v: Any) -> datetime | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        # .NET 給到 7 位小數：Python 只吃 6 位
        if "." in s:
            head, frac = s.split(".", 1)
            tz = "Z" if frac.endswith("Z") else ""
            frac = frac.rstrip("Z")[:6]
            s = f"{head}.{frac}{tz}"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_leases(leases: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """目前有效的租約（到期時間在現在之後；沒有到期時間的照算）。"""
    now = now or datetime.now(UTC)
    out = []
    for le in leases:
        if not isinstance(le, dict):
            continue
        ip = _ip4(le.get("address"))
        if not ip:
            continue
        ends = _when(le.get("leaseExpires"))
        if ends is not None and ends <= now:
            continue
        out.append({"ip": ip, "mac": _mac(le.get("hardwareAddress")), "hostname": le.get("hostName") or None,
                    "ends": ends.isoformat() if ends else None, "scope": le.get("scope")})
    out.sort(key=lambda x: ipaddress.ip_address(x["ip"]))
    return out


async def healthcheck(inst: Any) -> dict[str, Any]:
    """測試連線：版本、帳號、DHCP 權限、範圍與租約數量。沒有 DHCP 檢視權限就直接說。"""
    cli = client_for(inst)
    info = await cli.session()
    scopes = (await cli.call("/api/dhcp/scopes/list")).get("scopes") or []
    leases = (await cli.call("/api/dhcp/leases/list")).get("leases") or [] if inst.sync_leases else []
    return {"version": info["version"], "server": info["server"], "user": info["user"],
            "scopes": len(scopes), "enabled_scopes": sum(1 for s in scopes if isinstance(s, dict) and s.get("enabled")),
            "leases": len(parse_leases(leases)), "can_modify": bool(info["dhcp"].get("modify"))}


async def _write_scope_mirror(session: AsyncSession, inst: Any, parsed: list[dict[str, Any]], now: datetime) -> None:
    from app.models.technitium import TechnitiumDhcpScope
    rows = {r.name: r for r in (await session.execute(select(TechnitiumDhcpScope).where(
        TechnitiumDhcpScope.server_id == inst.id))).scalars().all()}
    names = set()
    for p in parsed:
        if not p["name"] or not p["start"] or not p["end"]:
            continue
        names.add(p["name"])
        row = rows.get(p["name"]) or TechnitiumDhcpScope(server_id=inst.id, name=p["name"])
        row.enabled, row.subnet_cidr, row.start_ip, row.end_ip = p["enabled"], p["subnet"], p["start"], p["end"]
        row.exclusions = p["exclusions"]
        row.server_address = p.get("server_address")
        row.router = p["router"]
        row.dns_servers, row.ntp_servers, row.wins_servers = p["dns_servers"], p["ntp_servers"], p["wins_servers"]
        row.domain_name, row.lease_seconds = p["domain_name"], p["lease_seconds"]
        row.reservations, row.synced_at = len(p["reservations"]), now
        session.add(row)
    gone = [n for n in rows if n not in names]
    if gone:
        await session.execute(delete(TechnitiumDhcpScope).where(
            TechnitiumDhcpScope.server_id == inst.id, in_values(TechnitiumDhcpScope.name, gone)))


async def sync_instance(session: AsyncSession, inst: Any) -> dict[str, Any]:
    """拉一次：範圍＋保留＋選項（sync_scopes）、租約（sync_leases）。讀不到就往上拋（作業顯示失敗）。"""
    from app.models.technitium import TechnitiumDhcpServer
    from app.services.dhcp_standalone import write_leases, write_pools, write_reservations

    now = datetime.now(UTC)
    try:
        cli = client_for(inst)
        info = await cli.session()
        counts: dict[str, Any] = {}
        summary: dict[str, Any] = {"version": info["version"], "server": info["server"]}
        parsed: list[dict[str, Any]] = []
        if inst.sync_scopes:
            listed = [s for s in (await cli.call("/api/dhcp/scopes/list")).get("scopes") or [] if isinstance(s, dict)]
            summary["scopes_truncated"] = len(listed) > MAX_SCOPES
            for s in listed[:MAX_SCOPES]:
                detail = await cli.call("/api/dhcp/scopes/get", name=str(s.get("name") or ""))
                one = parse_scope(detail, enabled=bool(s.get("enabled")))
                one["server_address"] = _ip4(s.get("interfaceAddress"))
                parsed.append(one)
        raw_leases = (await cli.call("/api/dhcp/leases/list")).get("leases") or [] if inst.sync_leases else None
    except TechnitiumError as exc:
        inst.last_error = str(exc)
        await session.commit()
        raise

    if inst.sync_scopes:
        live = [p for p in parsed if p["enabled"]]
        counts["scopes"] = len(parsed)
        counts["pools"] = await write_pools(
            session, source_type="technitium", source_id=inst.id, source_name=inst.name, engine=SOURCE,
            pools=[{"subnet": p["subnet"], "start": a, "end": b} for p in live for a, b in p["pools"]])
        counts["reservations"] = await write_reservations(
            session, source_type=SOURCE, source_id=inst.id, source_name=inst.name, engine=SOURCE,
            rows=[r for p in live for r in p["reservations"]])
        await _write_scope_mirror(session, inst, parsed, now)
    if raw_leases is not None:
        leases = parse_leases(raw_leases, now=now)
        counts["leases"] = await write_leases(
            session, source_type=SOURCE, source_id=inst.id, peers_model=TechnitiumDhcpServer,
            scope_ids=list(inst.scope_subnet_ids) if inst.scope_subnet_ids else None,
            leases=leases, complete=True)
        summary["lease_rows"] = len(leases)
    inst.last_summary = {**summary, **counts}
    inst.last_sync_at = datetime.now(UTC)
    inst.last_error = None
    return counts
