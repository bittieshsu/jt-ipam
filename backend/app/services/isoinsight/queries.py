"""ISOinsight 來源租約的查詢（REST `/isoinsight/leases` 與 AI 工具共用同一份）。

- 可見性推進 SQL：非管理員只看得到自己可見子網路內的租約（先過濾再分頁，總數也只算看得到的）
- 配對不到子網路的租約不屬於任何網路範圍：只有管理員看得到
- `state` 是查詢當下依時間推定的租約狀態，與 parser.lease_state 同一套規則 —— 不是設備上線狀態
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.models.isoinsight import IsoInsightLease, IsoInsightSource
from app.models.subnet import Subnet

LEASE_STATES = ("active", "expired", "not_started", "invalid_period", "unknown")


def lease_state_expr(now: datetime) -> Any:
    """依時間推定的租約狀態（查詢當下），與 parser.lease_state 同一套規則。"""
    L = IsoInsightLease
    return case(
        (and_(L.start_at.is_not(None), L.end_at.is_not(None), L.end_at < L.start_at), "invalid_period"),
        (and_(L.end_at.is_not(None), L.end_at <= now), "expired"),
        (and_(L.start_at.is_not(None), L.start_at > now), "not_started"),
        (and_(L.start_at.is_not(None), L.end_at.is_not(None), L.start_at <= now, L.end_at > now), "active"),
        else_="unknown")


async def query_leases(
    session: AsyncSession, *, user: Any, source_id: uuid.UUID | None = None, ip: str | None = None,
    mac: str | None = None, q: str | None = None, subnet_ids: list[uuid.UUID] | None = None,
    state: str | None = None, match_status: str | None = None, observed_since: datetime | None = None,
    observed_until: datetime | None = None, offset: int = 0, limit: int = 50,
) -> dict[str, Any]:
    """REST 與 AI 工具共用的查詢：可見性推進 SQL（不先 LIMIT 再過濾），回總數。"""
    from app.services.permission import visible_ids

    now = datetime.now(UTC)
    st = lease_state_expr(now).label("state")
    L = IsoInsightLease
    stmt = (select(L, st, IsoInsightSource.name, cast(Subnet.cidr, String))
            .join(IsoInsightSource, IsoInsightSource.id == L.source_id)
            .outerjoin(Subnet, Subnet.id == L.subnet_id))
    if not getattr(user, "is_admin", False):
        vis = await visible_ids(session, user=user, object_type="subnet")
        stmt = stmt.where(L.subnet_id.is_not(None))      # 配對不到子網路的不屬於任何網路範圍
        if vis is not None:
            stmt = stmt.where(in_values(L.subnet_id, vis))
    if source_id:
        stmt = stmt.where(L.source_id == source_id)
    if ip:
        stmt = stmt.where(func.host(L.ip) == ip.strip())
    if mac:
        hexs = "".join(ch for ch in mac.lower() if ch in "0123456789abcdef")
        stmt = stmt.where(L.mac_key == hexs) if len(hexs) == 12 else stmt.where(L.mac_key.contains(hexs))
    if q:
        term = q.strip()
        stmt = stmt.where(or_(L.name.icontains(term, autoescape=True),
                              func.host(L.ip).startswith(term, autoescape=True)))
    if subnet_ids:
        stmt = stmt.where(in_values(L.subnet_id, subnet_ids))
    if state:
        stmt = stmt.where(lease_state_expr(now) == state)
    if match_status:
        stmt = stmt.where(L.match_status == match_status)
    if observed_since:
        stmt = stmt.where(L.lease_observed_at >= observed_since)
    if observed_until:
        stmt = stmt.where(L.lease_observed_at <= observed_until)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (await session.execute(stmt.order_by(L.ip, L.mac_key).offset(offset).limit(limit))).all()
    items = []
    for le, state_v, src_name, cidr in rows:
        items.append({"id": le.id, "source_id": le.source_id, "source_name": src_name,
                      "ip": str(le.ip).split("/")[0], "mac": le.mac, "name": le.name,
                      "start_at": le.start_at, "end_at": le.end_at, "start_raw": le.start_raw,
                      "end_raw": le.end_raw, "state": state_v, "quality": list(le.quality or []),
                      "raw_count": le.raw_count, "match_status": le.match_status, "subnet_id": le.subnet_id,
                      "subnet_cidr": cidr, "ip_address_id": le.ip_address_id,
                      "first_observed_at": le.first_observed_at, "lease_observed_at": le.lease_observed_at})
    srcs = (await session.execute(select(IsoInsightSource.id, IsoInsightSource.name, IsoInsightSource.last_commit_at,
                                         IsoInsightSource.last_fetch_ok_at, IsoInsightSource.last_result)
                                  .order_by(IsoInsightSource.name))).all()
    return {"items": items, "total": total, "now": now,
            "sources": [{"id": i, "name": n, "last_commit_at": c, "last_fetch_ok_at": f, "last_result": r}
                        for i, n, c, f, r in srcs]}
