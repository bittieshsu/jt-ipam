"""04 DNS／DHCP／憑證：DNS 比對群組的來源一致性、DHCP 集區有效租約使用率、憑證剩餘天數與到期區間。

- DNS（管理員限定，與 DNS 比對群組頁同一條線）：一個子頁一個群組，每個來源（最多 MAX_SOURCES 台）一塊方格：
  一致（每台都有）／只有部分來源有（這台有、別台沒有）／這台缺少（已確認）／這台缺少（還在寬限時間內，待確認）。
  整個 zone 缺少算一格（差異表就是這樣記的）；格子太多時一格代表 N 筆並標示
- DHCP：與 02 容量頁同一份集區資料（capacity.pool_items，依看得到的子網路過濾），使用率高的先出現
- 憑證（管理員限定）：到期區間互斥（已過期／7 天內／8 到 14 天／15 到 30 天）；子頁輪流顯示 30 天內到期的憑證，
  最急的先出現；30 天內都沒有就顯示最近要到期的那一張
- 三塊各自分頁：第 N 個子頁顯示各自的第 N 項（項目比較少的那塊繞回第一項），總頁數取最多的那塊
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.services.warroom.common import scoped_visibility, section, unit_for, waffle

MAX_SOURCES = 4
SOURCE_CELLS = 96                    # 每個來源最多幾格（12 欄 × 8 列）
POOL_CELLS = 40                      # 集區條帶的格數
BUCKETS = (("expired", -math.inf, 0), ("d7", 0, 8), ("d14", 8, 15), ("d30", 15, 31))
STATUS_RANK = {"mismatch": 0, "pending": 1, "incomplete": 2, "ok": 3}
SOURCE_STATES = ("ok", "partial", "pending", "missing")


def _pick(items: list[Any], page: int) -> tuple[Any | None, int]:
    """第 page 個子頁要顯示的項目（項目少於頁數時繞回來）與它是第幾項（從 1 起）。"""
    if not items:
        return None, 0
    i = (page - 1) % len(items)
    return items[i], i + 1


async def _record_counts(session: AsyncSession, group: Any, member_ids: list[Any]) -> dict[Any, int]:
    """每台成員參與比對的紀錄數（與 dns_compare._snapshot 同一套排除規則：比對類型、不比對的 zone、AD 定位紀錄）。"""
    from app.models.dns import DNSRecord, DNSZone
    from app.services import dns_compare as dc

    types = tuple(dc.normalize_compare_types(group.compare_types) or dc.DEFAULT_COMPARE_TYPES)
    excluded = frozenset(dc.normalize_zone_list(group.excluded_zones))
    out: dict[Any, int] = defaultdict(int)
    rows = (await session.execute(
        select(DNSZone.server_id, DNSZone.name, func.count())
        .select_from(DNSRecord).join(DNSZone, DNSZone.id == DNSRecord.zone_id)
        .where(in_values(DNSZone.server_id, member_ids),
               func.upper(DNSRecord.type).in_(types),  # bounded: 至多 9 種記錄類型（COMPARE_TYPES）
               DNSRecord.name_norm.is_not(None), DNSRecord.value_norm.is_not(None),
               ~func.lower(DNSRecord.name_norm).op("~")(r"(^|\.)_msdcs(\.|$)|^(domaindnszones|forestdnszones)(\.|$)"))
        .group_by(DNSZone.server_id, DNSZone.name))).all()
    for sid, zname, n in rows:
        if dc.is_locator_zone(zname) or dc.normalize_zone(zname) in excluded:
            continue
        out[sid] += int(n)
    return out


async def _dns(session: AsyncSession, page: int) -> tuple[dict[str, Any], int, int]:
    """(段落, 群組數, 不一致的群組數)"""
    from app.models.dns import DNSServer
    from app.models.dns_compare_group import DNSCompareGroup, DNSCompareGroupDiff

    groups = list((await session.execute(select(DNSCompareGroup))).scalars().all())
    groups.sort(key=lambda g: (STATUS_RANK.get(g.last_status or "", 9), g.name))
    mismatch = sum(1 for g in groups if g.last_status == "mismatch")
    g, idx = _pick(groups, page)
    if g is None:
        return section("empty"), 0, 0
    members = list((await session.execute(select(DNSServer).where(
        DNSServer.compare_group_id == g.id, DNSServer.enabled.is_(True)).order_by(DNSServer.name))).scalars().all())
    diffs = list((await session.execute(select(DNSCompareGroupDiff).where(
        DNSCompareGroupDiff.group_id == g.id))).scalars().all())
    totals = await _record_counts(session, g, [m.id for m in members]) if members else {}
    sources = []
    for m in members[:MAX_SOURCES]:
        key = str(m.id)
        partial = sum(1 for d in diffs if d.kind == "record" and key in (d.present_on or []))
        missing = sum(1 for d in diffs if key in (d.missing_on or []) and d.confirmed_at is not None)
        pending = sum(1 for d in diffs if key in (d.missing_on or []) and d.confirmed_at is None)
        counts = {"ok": max(totals.get(m.id, 0) - partial, 0), "partial": partial,
                  "missing": missing, "pending": pending}
        total = sum(counts.values())
        unit = unit_for(total, SOURCE_CELLS)
        sources.append({
            "id": key, "name": m.name, "type": m.type, "counts": counts, "total": total, "unit": unit,
            "cells": waffle([(s, counts[s]) for s in SOURCE_STATES], math.ceil(total / unit)) if total else [],
            "last_sync_at": m.last_sync_at.isoformat() if m.last_sync_at else None,
        })
    return section("ok", index=idx, total=len(groups), group={
        "id": str(g.id), "name": g.name, "status": g.last_status,
        "checked_at": g.last_checked_at.isoformat() if g.last_checked_at else None,
        "members": len(members), "more_sources": max(len(members) - MAX_SOURCES, 0),
        "diffs": len(diffs), "confirmed": sum(1 for d in diffs if d.confirmed_at is not None),
        "sources": sources,
    }), len(groups), mismatch


async def _pools(session: AsyncSession, visible: set[Any] | None, cfg: dict[str, Any],
                 page: int) -> tuple[dict[str, Any], int, int]:
    from app.services.warroom.capacity import pool_items

    pools = await pool_items(session, visible, cfg["capacity_warn"], cfg["capacity_crit"])
    pools.sort(key=lambda p: (-(p["pct"] or 0), p["label"]))
    alert = sum(1 for p in pools if p["level"] == "crit")
    p, idx = _pick(pools, page)
    if p is None:
        return section("empty"), 0, 0
    unit = max(1, math.ceil(p["capacity"] / POOL_CELLS))
    p = {**p, "unit": unit, "cells": waffle([("allocated", p["used"]), ("free", p["free"])],
                                            math.ceil(p["capacity"] / unit))}
    return section("ok", index=idx, total=len(pools), item=p), len(pools), alert


async def _certs(session: AsyncSession, page: int, now: datetime) -> tuple[dict[str, Any], int, int]:
    """(段落, 子頁數, 30 天內到期（含已過期）的張數)"""
    from app.models.certificate import Certificate, CertVersion

    rows = (await session.execute(
        select(Certificate.id, Certificate.name, CertVersion.not_after)
        .join(CertVersion, CertVersion.certificate_id == Certificate.id)
        .where(CertVersion.is_current.is_(True)).order_by(CertVersion.not_after))).all()
    if not rows:
        return section("empty"), 0, 0
    certs = [{"id": str(cid), "name": name, "not_after": na.isoformat(),
              "days": math.floor((na - now).total_seconds() / 86400)} for cid, name, na in rows]
    buckets = dict.fromkeys((b[0] for b in BUCKETS), 0)
    for c in certs:
        for key, lo, hi in BUCKETS:
            if lo <= c["days"] < hi:
                buckets[key] += 1
                break
    soon = [c for c in certs if c["days"] < 31]
    pick_from = soon or certs[:1]
    c, idx = _pick(pick_from, page)
    return section("ok", buckets=buckets, item=c, index=idx, total=len(pick_from),
                   soon=len(soon), all=len(certs)), len(pick_from), len(soon)


async def build(session: AsyncSession, *, user: Any, cfg: dict[str, Any], page: int = 1,
                now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    from app.services.warroom.common import full_access
    admin = await full_access(session, user)
    visible = await scoped_visibility(session, user, cfg)
    pool, n_pools, pools_alert = await _pools(session, visible, cfg, page)
    if admin:
        dns, n_groups, mismatch = await _dns(session, page)
        certs, n_certs, certs_30d = await _certs(session, page, now)
    else:
        dns, n_groups, mismatch = section("forbidden"), 0, None
        certs, n_certs, certs_30d = section("forbidden"), 0, None
    pages = max(1, n_groups, n_pools, n_certs)
    page = min(max(1, page), pages)
    return {
        "key": "services", "data_at": now.isoformat(), "page": page, "pages": pages, "per_page": 1, "total": pages,
        "metrics": {"dns_mismatch": mismatch, "pools_alert": pools_alert, "certs_30d": certs_30d},
        "dns": dns, "pool": pool, "certs": certs,
    }
