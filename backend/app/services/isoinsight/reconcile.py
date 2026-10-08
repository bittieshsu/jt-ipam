"""ISOinsight 租約 → 子網路配對、來源觀察、套用到 IP 記錄（規格 §9）。

`plan()` 只讀（預覽與同步共用）；`apply()` 寫入（只在同步的交易裡呼叫）。

配對：
- 只在來源允許的子網路內配對，而且允許的子網路必須屬於來源設定的租戶（客戶）；改過歸屬、已封存的
  子網路在同步當下就排除（不跨租戶自動配對）
- 允許範圍內已有這個 IP（唯一）→ 用那一筆；有好幾筆（重疊）→ 不明確，不猜
- 沒有 → 最長首碼、唯一才算；同長度重疊 → 不明確；都不包含 → 不在允許範圍

套用（沿用既有規則，不另寫一套）：
- 只有「租約期間內、沒有 MAC 衝突、配對唯一」的那一筆會碰 IP 記錄；過期、時間不明、尚未開始、
  期間無效的只留在來源觀察
- MAC 走 MAC 來源優先序（`arp_precedence`）、主機名稱走主機名稱優先序（`HostnameRun`）：
  人工維護的值不會被蓋掉；MAC 不合法不寫、名稱空白不清
- 「有租約」旗標走 `LeaseRun`（逐來源目擊、旗標全域重算）
- 不記上線證據：租約有效不等於設備上線（不碰 last_seen／arp_seen）
- 新增正式 IP：唯一配對到允許子網路＋租約期間內＋來源開了 `create_ips`；冷卻期內、網路／廣播位址不建；
  IPv6 待真機驗證前只留觀察

不依「這次沒出現」推定釋放：上一輪看到、這輪沒出現但租約時間還沒到期的，照樣算目前租約（重新套用，
冪等）；到期了才不算。來源觀察本身只清「30 天沒再看到、而且已到期（或時間不明）」的列。
"""
from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values, not_in_values
from app.models.address import IPAddress
from app.models.isoinsight import IsoInsightLease, IsoInsightSource
from app.models.subnet import Subnet
from app.services.isoinsight.parser import TIME_PROBLEM_STATES, Lease, Parsed, pick_current

SOURCE = "isoinsight"
#: 來源觀察的保留：這麼久沒再看到、而且已到期（或時間不明）才清
OBSERVATION_RETENTION = timedelta(days=30)
#: 這些標籤的觀察，即使後來時間落在租約期間內，也不沿用到 IP 記錄
_NEVER_CARRY = ("mac_conflict", "not_started", "invalid_period", "time_unknown", "ipv6_unverified")
#: 處理結果（互斥；加總＝去重後的租約數）
BUCKETS = ("created", "updated", "unchanged", "observed_only", "expired", "unknown_time", "unmatched",
           "conflicts")


@dataclass(slots=True)
class Outcome:
    lease: Lease
    bucket: str
    reason: str | None = None              # observed_only／unmatched 的原因
    match_status: str = "no_subnet"
    subnet_id: uuid.UUID | None = None
    subnet_cidr: str | None = None
    ip_address_id: uuid.UUID | None = None
    applies: bool = False                  # 這一筆要套用到 IP 記錄
    create_in: uuid.UUID | None = None     # 要在這個子網路新增 IP


@dataclass(slots=True)
class Plan:
    outcomes: list[Outcome]
    scope_ids: list[uuid.UUID]
    scope_excluded: int
    by_ip_obj: dict[str, IPAddress] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(BUCKETS, 0)
        for o in self.outcomes:
            out[o.bucket] += 1
        return out


async def scope_nets(session: AsyncSession, src: IsoInsightSource) -> tuple[list[tuple[Any, uuid.UUID]],
                                                                            dict[uuid.UUID, str], int]:
    """允許的子網路（依首碼由長到短）、id → CIDR、被排除的數量（已封存或不屬於來源的租戶）。"""
    ids = list(src.scope_subnet_ids or [])
    if not ids:
        return [], {}, 0
    rows = (await session.execute(select(Subnet.id, Subnet.cidr, Subnet.customer_id, Subnet.archived_at)
                                  .where(in_values(Subnet.id, ids)))).all()
    nets: list[tuple[Any, uuid.UUID]] = []
    cidrs: dict[uuid.UUID, str] = {}
    excluded = len(ids) - len(rows)
    for sid, cidr, customer_id, archived_at in rows:
        if archived_at is not None or customer_id != src.customer_id:
            excluded += 1
            continue
        try:
            net = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError:
            excluded += 1
            continue
        nets.append((net, sid))
        cidrs[sid] = str(net)
    nets.sort(key=lambda x: x[0].prefixlen, reverse=True)
    return nets, cidrs, excluded


def _not_host(aip: Any, net: Any) -> bool:
    if net.version == 4:
        return bool(net.prefixlen <= 30 and aip in (net.network_address, net.broadcast_address))
    return net.prefixlen <= 126 and aip == net.network_address


async def plan(session: AsyncSession, src: IsoInsightSource, parsed: Parsed, *, now: datetime) -> Plan:
    """逐筆決定處理結果（不寫任何東西）。"""
    from app.models.ip_cooldown import IPCooldown
    from app.services.ip_autocreate import SubnetIndex, match_existing_many

    nets, cidrs, excluded = await scope_nets(session, src)
    scope_ids = [sid for _n, sid in nets]
    idx = SubnetIndex(nets)
    ips = {le.ip for le in parsed.leases}
    matches = await match_existing_many(session, ips, scope_ids) if scope_ids and ips else {}
    selection = pick_current(parsed.leases)

    # 逐 IP 的配對結果
    where: dict[str, tuple[str, uuid.UUID | None, IPAddress | None, Any]] = {}
    for ip in ips:
        aip = ipaddress.ip_address(ip)
        obj, ambiguous = matches.get(ip, (None, False))
        if obj is not None:
            net = next((n for n, sid in nets if sid == obj.subnet_id), None)
            where[ip] = ("matched", obj.subnet_id, obj, net)
        elif ambiguous:
            where[ip] = ("ambiguous", None, None, None)
        else:
            hit = idx.pick_net(aip) if nets else None
            if hit is not None:
                where[ip] = ("matched", hit[1], None, hit[0])
            elif nets and idx.longest(aip) is not None:
                where[ip] = ("ambiguous", None, None, None)
            else:
                where[ip] = ("no_subnet", None, None, None)

    # 會新增的候選：冷卻期（管理員剛釋放的位址）一次查完
    creatable = {le.ip: where[le.ip][1] for le in parsed.leases
                 if src.create_ips and le.version == 4 and where[le.ip][0] == "matched"
                 and where[le.ip][2] is None and selection[le.ip].current is le}
    cooling: set[tuple[Any, str]] = set()
    if creatable:
        host = func.host(IPCooldown.ip)
        cooling = {(sid, str(ip)) for sid, ip in (await session.execute(
            select(IPCooldown.subnet_id, host).where(
                in_values(host, set(creatable), type_=String()),
                IPCooldown.cleared_at.is_(None), IPCooldown.until > now))).all()}

    outcomes: list[Outcome] = []
    for le in parsed.leases:
        status, sid, obj, net = where[le.ip]
        sel = selection[le.ip]
        o = Outcome(lease=le, bucket="observed_only", match_status=status, subnet_id=sid,
                    subnet_cidr=cidrs.get(sid) if sid else None, ip_address_id=obj.id if obj else None)
        if "mac_conflict" in le.quality:
            o.bucket = "conflicts"
        elif le.state == "expired":
            o.bucket = "expired"
        elif le.state in TIME_PROBLEM_STATES:
            o.bucket = "unknown_time"
        elif status != "matched":
            o.bucket, o.reason = "unmatched", status
        elif sel.current is not le:
            o.reason = "superseded"
        elif le.version == 6:
            o.reason = "ipv6_pending"
        elif obj is not None:
            o.applies = True
            o.bucket = "unchanged"            # 套用後再依實際有沒有變分成 updated／unchanged
        elif not src.create_ips:
            o.reason = "create_disabled"
        elif net is not None and _not_host(ipaddress.ip_address(le.ip), net):
            o.reason = "not_host"
        elif (sid, le.ip) in cooling:
            o.reason = "cooldown"
        else:
            o.applies = True
            o.create_in = sid
            o.bucket = "created"
        outcomes.append(o)
    return Plan(outcomes=outcomes, scope_ids=scope_ids, scope_excluded=excluded,
                by_ip_obj={ip: w[2] for ip, w in where.items() if w[2] is not None})


async def _snapshot(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[Any, ...]]:
    if not ids:
        return {}
    rows = (await session.execute(text(
        "SELECT id, mac::text, hostname, in_dhcp_lease FROM ip_addresses WHERE id = ANY(:ids)"),
        {"ids": list(ids)})).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def apply(session: AsyncSession, src: IsoInsightSource, parsed: Parsed, pl: Plan, *,
                now: datetime) -> dict[str, Any]:
    """寫入：新增 IP、來源觀察、套用到 IP 記錄。呼叫端負責交易（失敗就整批 rollback）。"""
    from app.services.dhcp_leases import LeaseRun
    from app.services.fw_sightings import SightingBatch
    from app.services.hostname_reports import HostnameRun, enabled_peers
    from app.services.ip_history import log_change

    when = now.astimezone().strftime("%Y-%m-%d %H:%M")

    # 1. 新增 IP（唯一配對、租約期間內）
    created: list[IPAddress] = []
    for o in pl.outcomes:
        if o.create_in is None:
            continue
        obj = IPAddress(subnet_id=o.create_in, ip=o.lease.ip, state="used", discovery_source=SOURCE,
                        note=f"此 IP 由 ISOinsight 整合「{src.name}」於 {when} 依 DHCP 租約自動建立。")
        session.add(obj)
        created.append(obj)
        pl.by_ip_obj[o.lease.ip] = obj
    if created:
        await session.flush()          # autoflush=False：先取得 id，來源觀察與異動記錄才有對象
        for obj in created:
            await log_change(session, ip=obj, event_type="created", source=SOURCE,
                             note="依 ISOinsight DHCP 租約自動建立")
        for o in pl.outcomes:
            if o.create_in is not None:
                o.ip_address_id = pl.by_ip_obj[o.lease.ip].id

    # 2. 來源觀察：本輪回應的每一筆（空白名稱不蓋掉上次看到的名稱）
    rows = [{
        "id": uuid.uuid4(), "source_id": src.id, "ip": o.lease.ip, "mac_key": o.lease.mac_key, "mac": o.lease.mac,
        "name": o.lease.name, "start_at": o.lease.start, "end_at": o.lease.end,
        "start_raw": o.lease.start_raw, "end_raw": o.lease.end_raw, "quality": sorted(o.lease.quality),
        "raw_count": o.lease.raw_count, "match_status": o.match_status, "subnet_id": o.subnet_id,
        "ip_address_id": o.ip_address_id, "first_observed_at": now, "lease_observed_at": now,
    } for o in pl.outcomes]
    if rows:
        ins = pg_insert(IsoInsightLease)
        upsert = ins.on_conflict_do_update(
            constraint="uq_isoinsight_lease",
            set_={"mac": ins.excluded.mac,
                  "name": func.coalesce(ins.excluded.name, IsoInsightLease.name),
                  "start_at": ins.excluded.start_at, "end_at": ins.excluded.end_at,
                  "start_raw": ins.excluded.start_raw, "end_raw": ins.excluded.end_raw,
                  "quality": ins.excluded.quality, "raw_count": ins.excluded.raw_count,
                  "match_status": ins.excluded.match_status, "subnet_id": ins.excluded.subnet_id,
                  "ip_address_id": ins.excluded.ip_address_id,
                  "lease_observed_at": ins.excluded.lease_observed_at})
        for i in range(0, len(rows), 2000):
            await session.execute(upsert, rows[i:i + 2000])

    # 3. 目前的租約：本輪套用的＋上一輪看過、這輪沒出現但租約還沒到期的（不依缺席推定釋放）
    seen_ips = {o.lease.ip for o in pl.outcomes}
    applying = [o for o in pl.outcomes if o.applies]
    names: dict[str, str | None] = {}
    need_name = [o.lease.ip for o in applying if not o.lease.name]
    if need_name:
        for ip, mk, nm in (await session.execute(
                select(func.host(IsoInsightLease.ip), IsoInsightLease.mac_key, IsoInsightLease.name).where(
                    IsoInsightLease.source_id == src.id,
                    in_values(func.host(IsoInsightLease.ip), need_name, type_=String())))).all():
            names[f"{ip}|{mk}"] = nm
    cands: list[tuple[str, str | None, str | None]] = []
    for o in applying:
        nm = o.lease.name or names.get(f"{o.lease.ip}|{o.lease.mac_key}")
        cands.append((o.lease.ip, o.lease.mac, nm))
    carried = await _carried_over(session, src, seen_ips, pl.scope_ids, now=now)
    cands.extend(carried)

    # 4. 套用：MAC 與主機名稱照既有優先序、租約旗標逐來源目擊
    cand_ips = {ip for ip, _m, _n in cands}
    before_ids = {pl.by_ip_obj[ip].id for ip in cand_ips if ip in pl.by_ip_obj}
    await session.flush()
    before = await _snapshot(session, before_ids)
    lease_run = LeaseRun(session, source_type="isoinsight", source_id=src.id)
    hn_run = HostnameRun(session, source=SOURCE, origin=f"{SOURCE}:{src.id}",
                         peers=await enabled_peers(session, IsoInsightSource))
    if pl.scope_ids:
        batch = SightingBatch(session, source=SOURCE, subnet_ids=pl.scope_ids, lease_run=lease_run, hn_run=hn_run)
        for ip, mac, nm in cands:
            # evidence=None：租約只說「發給過誰」，不是上線證據；名稱比照其他 DHCP 來源只取主機部分
            batch.add(ip, evidence=None, mac=mac, hostname=nm.split(".")[0] if nm else None)
        await batch.flush()
    lease_out = await lease_run.finish(complete=True)
    hn_out = await hn_run.finish(complete=True)

    # 5. updated／unchanged：比對套用前後（MAC、主機名稱、有租約旗標）
    await session.flush()
    after = await _snapshot(session, before_ids)
    for o in applying:
        if o.bucket != "unchanged" or o.ip_address_id is None:
            continue
        if before.get(o.ip_address_id) != after.get(o.ip_address_id):
            o.bucket = "updated"

    # 6. 來源觀察的保留（依時間，不依缺席）
    pruned = (await session.execute(delete(IsoInsightLease).where(
        IsoInsightLease.source_id == src.id,
        IsoInsightLease.lease_observed_at < now - OBSERVATION_RETENTION,
        (IsoInsightLease.end_at.is_(None)) | (IsoInsightLease.end_at < now)))).rowcount or 0
    return {"carried_over": len(carried), "lease_flags": lease_out, "hostnames": hn_out,
            "observations_pruned": pruned}


async def _carried_over(session: AsyncSession, src: IsoInsightSource, seen_ips: set[str],
                        scope_ids: list[uuid.UUID], *, now: datetime) -> list[tuple[str, str | None, str | None]]:
    """上一輪看過、這輪沒出現、租約時間還沒到期、仍在允許範圍內的 → (ip, mac, 名稱)。

    同一個 IP 留著好幾個不同 MAC 的有效租約 → 衝突，不套用。
    """
    if not scope_ids:
        return []
    stmt = (select(func.host(IsoInsightLease.ip), IsoInsightLease.mac_key, IsoInsightLease.mac,
                   IsoInsightLease.name, IsoInsightLease.start_at)
            .join(IPAddress, IPAddress.id == IsoInsightLease.ip_address_id)
            .where(IsoInsightLease.source_id == src.id,
                   IsoInsightLease.start_at.is_not(None), IsoInsightLease.start_at <= now,
                   IsoInsightLease.end_at.is_not(None), IsoInsightLease.end_at > now,
                   # 當時就不會套用的（衝突、時間有疑義、IPv6 待驗證），時間過去了也不會變成可以套用
                   ~IsoInsightLease.quality.overlap(list(_NEVER_CARRY)),
                   in_values(IPAddress.subnet_id, scope_ids)))
    if seen_ips:
        stmt = stmt.where(not_in_values(func.host(IsoInsightLease.ip), seen_ips, type_=String()))
    by_ip: dict[str, list[tuple[str, str | None, str | None, datetime]]] = {}
    for ip, mk, mac, nm, start in (await session.execute(stmt)).all():
        by_ip.setdefault(str(ip), []).append((mk, mac, nm, start))
    out: list[tuple[str, str | None, str | None]] = []
    for ip, group in by_ip.items():
        macs = {mk for mk, *_r in group if mk}
        if len(macs) > 1:
            continue
        pool = [g for g in group if g[0]] or group
        mk, mac, nm, _s = max(pool, key=lambda g: g[3])
        out.append((ip, mac, nm))
    return out
