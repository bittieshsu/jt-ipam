"""外部 DNS 解析：對外開放服務的 FQDN 從網際網路上看是什麼（使用者 2026-10-19 定案）。

規格：docs/SPEC_EXTERNAL_DNS_zh-TW.md（內部文件）。這個模組是核心：
- 設定（system_settings `dns_resolvers`）的驗證與讀寫
- 只查「對外網域」底下的名稱 —— 內部名稱送去公共解析器等於洩漏內部架構
- 對設定的解析器查 A／AAAA（含 CNAME 鏈）；另外直接問網域的權威伺服器（不經快取）
- 把各解析器的答案、我方公網位址、對外開放清單合起來判定狀態

之後 DNS 查詢工具、AI 工具、懸空 DNS、IP 變更評估都走這裡，不要各自再寫一份查詢。
"""
from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

SETTINGS_KEY = "dns_resolvers"
MAX_RESOLVERS = 10
MAX_DOMAINS = 50
MAX_RANGES = 100
QUERY_TIMEOUT = 3.0
CONCURRENCY = 10
#: 權威伺服器最多問幾台（一個網域常有 2～4 台 NS，問兩台就看得出不一致）
AUTH_NS_MAX = 2

#: 外部狀態（畫面翻譯：surface.ext_st_<值>）
STATUSES = ("consistent", "not_public", "foreign", "mismatch", "no_path", "query_failed",
            "outside_domains", "not_checked")


# ── 設定 ─────────────────────────────────────────────────────────────────────

def _clean_domain(v: Any) -> str | None:
    d = str(v or "").strip().lower().rstrip(".")
    if d.startswith("*."):
        d = d[2:]
    if not d or len(d) > 253 or " " in d or "/" in d or "." not in d:
        return None
    return d


def _clean_range(v: Any) -> str | None:
    try:
        return str(ipaddress.ip_network(str(v).strip(), strict=False))
    except ValueError:
        return None


def normalize_settings(raw: Any) -> dict[str, Any]:
    """存進資料庫前、讀出來之後都過一次：不合格的項目丟掉、數量有上限。"""
    raw = raw if isinstance(raw, dict) else {}
    resolvers: list[dict[str, Any]] = []
    for r in (raw.get("resolvers") or [])[:MAX_RESOLVERS]:
        if not isinstance(r, dict):
            continue
        try:
            addr = str(ipaddress.ip_address(str(r.get("address") or "").strip()))
        except ValueError:
            continue
        try:
            port = int(r.get("port") or 53)
        except (TypeError, ValueError):
            port = 53
        resolvers.append({"name": (str(r.get("name") or "").strip() or addr)[:60], "address": addr,
                          "port": port if 1 <= port <= 65535 else 53, "enabled": r.get("enabled", True) is not False})
    domains = list(dict.fromkeys(d for d in (_clean_domain(x) for x in raw.get("public_domains") or []) if d))
    ranges = list(dict.fromkeys(x for x in (_clean_range(v) for v in raw.get("public_ranges") or []) if x))
    try:
        hours = int(raw["interval_hours"]) if raw.get("interval_hours") is not None else 6
    except (TypeError, ValueError):
        hours = 6
    return {
        "resolvers": resolvers,
        "public_domains": domains[:MAX_DOMAINS],
        "public_ranges": ranges[:MAX_RANGES],
        "query_authoritative": raw.get("query_authoritative", True) is not False,
        "interval_hours": max(1, min(168, hours)),
        "notify_on_change": raw.get("notify_on_change", True) is not False,
    }


async def get_settings(session: AsyncSession) -> dict[str, Any]:
    from app.models.system_setting import SystemSetting
    row = await session.get(SystemSetting, SETTINGS_KEY)
    return normalize_settings(row.value if row else None)


async def save_settings(session: AsyncSession, data: dict[str, Any], *, user_id: Any) -> dict[str, Any]:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.system_setting import SystemSetting
    val = normalize_settings(data)
    row = await session.get(SystemSetting, SETTINGS_KEY)
    if row is None:
        row = SystemSetting(key=SETTINGS_KEY, value=val, updated_by=user_id)
        session.add(row)
    else:
        row.value = val
        row.updated_by = user_id
        flag_modified(row, "value")
    return val


def in_public_domains(fqdn: str, domains: list[str]) -> bool:
    """名稱是不是在對外網域底下（本身或子網域）。不在就不送去外部查。"""
    name = fqdn.strip().lower().rstrip(".")
    return any(name == d or name.endswith("." + d) for d in domains)


# ── 查詢 ─────────────────────────────────────────────────────────────────────

@dataclass
class Answer:
    resolver: str                 # 解析器名稱，或 authoritative:<NS 名稱>
    rcode: str                    # NOERROR／NXDOMAIN／NODATA／SERVFAIL／TIMEOUT／BLOCKED／ERROR
    a: list[str] = field(default_factory=list)
    aaaa: list[str] = field(default_factory=list)
    cname: list[str] = field(default_factory=list)
    ttl: int | None = None
    authoritative: bool = False
    error: str | None = None

    def addresses(self) -> list[str]:
        return sorted(set(self.a) | set(self.aaaa))

    def as_dict(self) -> dict[str, Any]:
        return {"resolver": self.resolver, "rcode": self.rcode, "a": self.a, "aaaa": self.aaaa,
                "cname": self.cname, "ttl": self.ttl, "authoritative": self.authoritative, "error": self.error}


def _cname_chain(response: Any) -> list[str]:
    import dns.rdatatype
    out: list[str] = []
    for rrset in getattr(response, "answer", []) or []:
        if rrset.rdtype == dns.rdatatype.CNAME:
            out.extend(str(r.target).rstrip(".") for r in rrset)
    return out


async def query_resolver(name: str, address: str, port: int, label: str) -> Answer:
    """對指定的解析器查 A 與 AAAA。逾時、拒答都記成 rcode，不丟例外（一台壞掉不影響其他台）。"""
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    from app.core.net_guard import check_target
    from app.core.safe_http import UnsafeOutboundURL
    try:
        check_target(address, port)        # 管理員設定的位址：照整合的規則（擋中繼資料等）
    except UnsafeOutboundURL as exc:
        return Answer(label, "BLOCKED", error=str(exc)[:200])
    res = dns.asyncresolver.Resolver(configure=False)
    res.nameservers = [address]
    res.port = port
    res.timeout = res.lifetime = QUERY_TIMEOUT
    out = Answer(label, "NOERROR")
    nx = nodata = 0
    for rdtype, bucket in (("A", out.a), ("AAAA", out.aaaa)):
        try:
            ans = await res.resolve(name, rdtype, raise_on_no_answer=False)
        except dns.resolver.NXDOMAIN:
            nx += 1
            continue
        except dns.exception.Timeout:
            return Answer(label, "TIMEOUT", error="timeout")
        except dns.resolver.NoNameservers as exc:
            return Answer(label, "SERVFAIL", error=str(exc)[:200])
        except dns.exception.DNSException as exc:
            return Answer(label, "ERROR", error=f"{type(exc).__name__}: {exc}"[:200])
        if ans.rrset is None:
            nodata += 1
        else:
            bucket.extend(sorted({r.to_text() for r in ans.rrset}))
            out.ttl = ans.rrset.ttl if out.ttl is None else min(out.ttl, ans.rrset.ttl)
        for c in _cname_chain(ans.response):
            if c not in out.cname:
                out.cname.append(c)
    if not out.addresses():
        out.rcode = "NXDOMAIN" if nx == 2 else "NODATA"
    return out


async def query_authoritative(name: str, via_address: str, via_port: int) -> list[Answer]:
    """找名稱所在 zone 的 NS → 解析 NS 位址 → 直接問（不要求遞迴）。看到的是最新的答案，不經快取。

    NS 位址來自 DNS（外面的人改得到）→ 用 strict 規則檢查，私有／中繼資料位址一律不連。"""
    import dns.asyncquery
    import dns.asyncresolver
    import dns.exception
    import dns.flags
    import dns.message
    import dns.rcode
    import dns.rdatatype
    import dns.resolver

    from app.core.net_guard import check_target
    from app.core.safe_http import UnsafeOutboundURL

    res = dns.asyncresolver.Resolver(configure=False)
    res.nameservers = [via_address]
    res.port = via_port
    res.timeout = res.lifetime = QUERY_TIMEOUT
    try:
        zone = await dns.asyncresolver.zone_for_name(name, resolver=res)
        ns_names = sorted(str(r.target).rstrip(".") for r in await res.resolve(zone, "NS"))[:AUTH_NS_MAX]
    except dns.exception.DNSException as exc:
        return [Answer("authoritative", "ERROR", authoritative=True, error=f"{type(exc).__name__}: {exc}"[:200])]

    async def one(ns: str) -> Answer:
        label = f"authoritative:{ns}"
        try:
            ip = (await res.resolve(ns, "A"))[0].to_text()
            check_target(ip, 53, policy="strict")
        except UnsafeOutboundURL as exc:
            return Answer(label, "BLOCKED", authoritative=True, error=str(exc)[:200])
        except dns.exception.DNSException as exc:
            return Answer(label, "ERROR", authoritative=True, error=f"{type(exc).__name__}: {exc}"[:200])
        out = Answer(label, "NOERROR", authoritative=True)
        for rdtype, bucket in ((dns.rdatatype.A, out.a), (dns.rdatatype.AAAA, out.aaaa)):
            q = dns.message.make_query(name, rdtype)
            q.flags &= ~dns.flags.RD          # 權威查詢：不要求遞迴
            try:
                r = await dns.asyncquery.udp(q, ip, timeout=QUERY_TIMEOUT)
                if r.flags & dns.flags.TC:
                    r = await dns.asyncquery.tcp(q, ip, timeout=QUERY_TIMEOUT)
            except dns.exception.Timeout:
                return Answer(label, "TIMEOUT", authoritative=True, error="timeout")
            except (dns.exception.DNSException, OSError) as exc:
                return Answer(label, "ERROR", authoritative=True, error=f"{type(exc).__name__}: {exc}"[:200])
            if r.rcode() == dns.rcode.NXDOMAIN:
                out.rcode = "NXDOMAIN"
                continue
            for rrset in r.answer:
                if rrset.rdtype == rdtype:
                    bucket.extend(sorted({x.to_text() for x in rrset}))
                    out.ttl = rrset.ttl if out.ttl is None else min(out.ttl, rrset.ttl)
            for c in _cname_chain(r):
                if c not in out.cname:
                    out.cname.append(c)
        if out.rcode == "NOERROR" and not out.addresses() and not out.cname:
            out.rcode = "NODATA"
        return out

    return list(await asyncio.gather(*(one(ns) for ns in ns_names)))


async def resolve_name(name: str, settings: dict[str, Any]) -> list[Answer]:
    """一個名稱：每個啟用的解析器各查一次，設定要求時再問權威伺服器。"""
    enabled = [r for r in settings["resolvers"] if r["enabled"]]
    tasks = [query_resolver(name, r["address"], r["port"], r["name"]) for r in enabled]
    out = list(await asyncio.gather(*tasks))
    if settings.get("query_authoritative") and enabled:
        out.extend(await query_authoritative(name, enabled[0]["address"], enabled[0]["port"]))
    return out


# ── 判定 ─────────────────────────────────────────────────────────────────────

@dataclass
class OwnAddress:
    """我方公網位址：哪台整合的介面、或設定的公網網段。"""
    label: str                     # 例：fw-a（OPNsense）、或 203.0.113.0/24
    firewall: str | None = None    # 整合實例名稱（拿來比對對外開放清單的防火牆欄）


def owner_of(ip: str, own: dict[str, OwnAddress], ranges: list[str]) -> OwnAddress | None:
    if ip in own:
        return own[ip]
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for r in ranges:
        net = ipaddress.ip_network(r, strict=False)
        if a.version == net.version and a in net:
            return OwnAddress(label=r)
    return None


def classify(answers: list[dict[str, Any]], *, own: dict[str, OwnAddress], ranges: list[str],
             row_firewalls: set[str]) -> dict[str, Any]:
    """各解析器的答案 → 外部狀態、公網位址與它們屬於誰。

    - 遞迴解析器（不含權威）之間答案不同、或權威伺服器有位址且跟它們不同 → mismatch
    - 都沒有位址：都是 NXDOMAIN／NODATA → not_public；有逾時／錯誤而沒有任何成功 → query_failed
    - 位址都不是我方 → foreign（CDN／雲端，或懸空記錄 → 子網域接管風險）
    - 是我方某台防火牆的位址，但對外開放清單裡這個名稱沒有經過那台 → no_path
    """
    if not answers:
        return {"status": "not_checked", "addresses": [], "owners": {}}
    ok = [a for a in answers if a["rcode"] in ("NOERROR", "NXDOMAIN", "NODATA")]
    rec = [a for a in ok if not a.get("authoritative")]
    auth = [a for a in ok if a.get("authoritative")]
    sets = {tuple(sorted(set(a["a"]) | set(a["aaaa"]))) for a in rec}
    addrs = sorted({x for s in sets for x in s})
    auth_sets = {tuple(sorted(set(a["a"]) | set(a["aaaa"]))) for a in auth if a["a"] or a["aaaa"]}
    owners = {ip: (o.label if o else None) for ip in addrs
              for o in [owner_of(ip, own, ranges)]}
    if not ok:
        return {"status": "query_failed", "addresses": [], "owners": {}}
    if len(sets) > 1 or (auth_sets and sets and any(s not in sets for s in auth_sets)):
        return {"status": "mismatch", "addresses": addrs, "owners": owners}
    if not addrs:
        if auth_sets:     # 只有權威有答案（解析器快取了「不存在」）也算不一致
            return {"status": "mismatch", "addresses": sorted({x for s in auth_sets for x in s}), "owners": {}}
        return {"status": "not_public", "addresses": [], "owners": {}}
    mine = [owner_of(ip, own, ranges) for ip in addrs]
    if not any(mine):
        return {"status": "foreign", "addresses": addrs, "owners": owners}
    fws = {o.firewall for o in mine if o and o.firewall}
    if fws and row_firewalls and not (fws & row_firewalls):
        return {"status": "no_path", "addresses": addrs, "owners": owners}
    return {"status": "consistent", "addresses": addrs, "owners": owners}


# ── 查詢一輪、存結果、找變動 ─────────────────────────────────────────────────

STATE_KEY = "dns_resolvers_state"
#: 一輪最多查幾個名稱（對外網域底下、對外開放清單裡有的名稱；實務上幾十到幾百個）
MAX_NAMES = 1000
CHANGE_KEEP_DAYS = 90
#: 自身位址算「防火牆」的來源：比對對外開放清單的防火牆欄（LibreNMS／SNMP 只代表「是我方的」）
FIREWALL_SOURCES = ("opnsense", "pfsense", "fortigate", "mikrotik", "paloalto", "checkpoint")


async def get_state(session: AsyncSession) -> dict[str, Any]:
    from app.models.system_setting import SystemSetting
    row = await session.get(SystemSetting, STATE_KEY)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


async def _set_state(session: AsyncSession, **kw: Any) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.system_setting import SystemSetting
    row = await session.get(SystemSetting, STATE_KEY)
    if row is None:
        session.add(SystemSetting(key=STATE_KEY, value=kw))
    else:
        row.value = {**(row.value or {}), **kw}
        flag_modified(row, "value")
    await session.flush()


def configured(settings: dict[str, Any]) -> bool:
    return bool(settings["public_domains"]) and any(r["enabled"] for r in settings["resolvers"])


async def surface_fqdns(session: AsyncSession) -> dict[str, set[str]]:
    """對外開放清單裡的 FQDN → 經過哪些防火牆（判定 no_path 用）。"""
    from app.services.fw_lookup import attack_surface
    out: dict[str, set[str]] = {}
    for it in await attack_surface(session, limit=None, nat_limit=None):
        for f in (it.get("identity") or {}).get("fqdns") or []:
            out.setdefault(str(f).lower().rstrip("."), set()).update([it["firewall"]] if it.get("firewall") else [])
    return out


async def own_address_map(session: AsyncSession) -> dict[str, OwnAddress]:
    from sqlalchemy import func, select

    from app.models.external_dns import IntegrationOwnAddress
    out: dict[str, OwnAddress] = {}
    for addr, src, name in (await session.execute(
            select(func.host(IntegrationOwnAddress.address), IntegrationOwnAddress.source_type,
                   IntegrationOwnAddress.instance_name))).all():
        fw = name if src in FIREWALL_SOURCES else None
        cur = out.get(addr)
        if cur is None or (fw and not cur.firewall):       # 同一個位址：防火牆的說法優先
            out[addr] = OwnAddress(label=f"{name or src}（{src}）", firewall=fw)
    return out


def _recursive_addresses(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({x for r in rows if not r.get("authoritative") and r["rcode"] == "NOERROR"
                   for x in [*r["a"], *r["aaaa"]]})


async def run_round(session: AsyncSession) -> dict[str, Any]:
    """查一輪：對外網域底下的名稱 × 啟用的解析器（＋權威）。結果覆蓋舊的，公網位址變了就記錄並通知。"""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete, select
    from sqlalchemy.dialects.postgresql import insert

    from app.models.external_dns import ExternalDnsChange, ExternalDnsResult

    settings = await get_settings(session)
    started = datetime.now(UTC)
    if not configured(settings):
        await _set_state(session, last_run_at=started.isoformat(), last_summary={"skipped": "not_configured"})
        return {"skipped": "not_configured"}
    names = sorted(n for n in await surface_fqdns(session) if in_public_domains(n, settings["public_domains"]))
    truncated = len(names) > MAX_NAMES
    names = names[:MAX_NAMES]

    prev: dict[str, list[dict[str, Any]]] = {}
    for r in (await session.execute(select(ExternalDnsResult))).scalars().all():
        prev.setdefault(r.fqdn, []).append({"rcode": r.rcode, "a": r.a, "aaaa": r.aaaa,
                                            "authoritative": r.authoritative})
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(n: str) -> tuple[str, list[Answer]]:
        async with sem:
            return n, await resolve_name(n, settings)

    results = await asyncio.gather(*(one(n) for n in names))
    changes: list[dict[str, Any]] = []
    failed = queries = 0
    for name, answers in results:
        for a in answers:
            queries += 1
            failed += a.rcode in ("TIMEOUT", "SERVFAIL", "ERROR", "BLOCKED")
            stmt = insert(ExternalDnsResult).values(
                fqdn=name, resolver=a.resolver[:160], rcode=a.rcode, a=a.a, aaaa=a.aaaa, cname=a.cname, ttl=a.ttl,
                authoritative=a.authoritative, error=a.error, checked_at=started)
            await session.execute(stmt.on_conflict_do_update(
                constraint="uq_external_dns_results",
                set_={k: stmt.excluded[k] for k in ("rcode", "a", "aaaa", "cname", "ttl", "authoritative",
                                                     "error", "checked_at")}))
        new_rows = [x.as_dict() for x in answers]
        old_set, new_set = _recursive_addresses(prev.get(name, [])), _recursive_addresses(new_rows)
        # 第一次查到不算變動；這輪全部查不到（逾時等）也不算 —— 不然連不上外網就會報「全部變了」
        if name in prev and old_set != new_set and any(x["rcode"] in ("NOERROR", "NXDOMAIN", "NODATA")
                                                       for x in new_rows if not x["authoritative"]):
            changes.append({"fqdn": name, "old": old_set, "new": new_set})
            session.add(ExternalDnsChange(fqdn=name, old=old_set, new=new_set, changed_at=started))
    # 這輪沒查的（名稱不在清單了、解析器拿掉了）不留舊答案
    await session.execute(delete(ExternalDnsResult).where(ExternalDnsResult.checked_at < started))
    await session.execute(delete(ExternalDnsChange).where(
        ExternalDnsChange.changed_at < started - timedelta(days=CHANGE_KEEP_DAYS)))
    summary = {"names": len(names), "queries": queries, "failed": failed, "changed": len(changes),
               "truncated": truncated}
    await _set_state(session, last_run_at=started.isoformat(), last_summary=summary)
    if changes and settings["notify_on_change"]:
        from app.services.notification import notify_admins_event
        shown = "、".join(c["fqdn"] for c in changes[:5]) + ("…" if len(changes) > 5 else "")
        await notify_admins_event(
            session, event="external_dns.changed", severity="warning",
            title=f"外部 DNS 解析變動：{len(changes)} 個名稱", body=shown,
            link="/attack-surface?tab=fqdn", object_type="system",
            title_key="notif.external_dns_changed", body_key="notif.external_dns_changed_body",
            params={"n": len(changes), "names": shown})
    return summary


async def view(session: AsyncSession) -> dict[str, Any]:
    """對外開放服務頁 FQDN 頁籤用：每個名稱的外部答案與判定。"""
    from sqlalchemy import select

    from app.models.external_dns import ExternalDnsResult

    settings = await get_settings(session)
    state = await get_state(session)
    fqdns = await surface_fqdns(session)
    own = await own_address_map(session)
    rows: dict[str, list[ExternalDnsResult]] = {}
    for r in (await session.execute(select(ExternalDnsResult).order_by(
            ExternalDnsResult.fqdn, ExternalDnsResult.authoritative, ExternalDnsResult.resolver))).scalars().all():
        rows.setdefault(r.fqdn, []).append(r)
    out: dict[str, Any] = {}
    for name, fws in fqdns.items():
        if not in_public_domains(name, settings["public_domains"]):
            out[name] = {"status": "outside_domains", "answers": [], "addresses": [], "owners": {}, "checked_at": None}
            continue
        ans = [{"resolver": r.resolver, "rcode": r.rcode, "a": r.a, "aaaa": r.aaaa, "cname": r.cname, "ttl": r.ttl,
                "authoritative": r.authoritative, "error": r.error} for r in rows.get(name, [])]
        cls = classify(ans, own=own, ranges=settings["public_ranges"], row_firewalls=fws)
        checked = max((r.checked_at for r in rows.get(name, [])), default=None)
        out[name] = {**cls, "answers": ans, "checked_at": checked.isoformat() if checked else None}
    return {"configured": configured(settings), "public_domains": settings["public_domains"],
            "last_run_at": state.get("last_run_at"), "last_summary": state.get("last_summary"), "names": out}
