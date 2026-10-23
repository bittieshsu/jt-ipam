"""03 網路關聯：以一台設備為中心的正交關係圖（上一層、這台、下一層），右側是這台的資料與連線。

- 資料來自拓樸頁同一份 build_topology（同一套權限：只畫看得到的裝置、兩端都看得到的連線），
  只取裝置之間的連線：實體佈線、無線、FDB 推得的存取層與骨幹、鄰居探索
- 上下層：每個相連的群組選一個根（防火牆 > 路由器 > 交換器 > 其他，同類型連線多的優先），
  以到根的距離分層：比這台近的是上一層、遠的是下一層、一樣遠的列在下一層並標「同層」
- 中心設備輪播：有未處理異常的設備優先（管理員才看得到異常），其餘依連線數由多到少；每個子頁一台
- 線型照證據：人為登記與監控平台回報是實線，被動學到（FDB）與推導是虛線
- 名稱一律照原樣（不自創 SW-001 之類的短碼）；類型只當標籤
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sqlin import in_values
from app.services.warroom.common import actionable_observation, section

DEVICE_EDGE_KINDS = ("cable", "wireless", "l2", "l2_uplink")
#: 網路關聯只畫網路設備（使用者 10-22：預設不要伺服器或一般裝置）；接在下面的終端只算數量
NETWORK_TYPES = frozenset({"firewall", "router", "switch", "ap"})
MAX_UP = 3
MAX_DOWN = 6
MAX_LINKS = 6
MAX_FOCUS = 50
TYPE_RANK = {"firewall": 0, "router": 1, "switch": 2}
EV_RANK = {"asserted": 3, "monitored": 2, "learned": 1, "inferred": 0}
SOLID = ("asserted", "monitored")


def _line(ev: str) -> str:
    return "solid" if ev in SOLID else "dashed"


async def _graph(session: AsyncSession, user: Any, limited: bool, subnet_ids: list[Any] | None = None) -> tuple[dict[str, dict[str, Any]], dict[
        str, dict[str, dict[str, Any]]], dict[str, Any] | None]:
    """(裝置 id → 節點資料, 鄰接：a → b → {evidence, port}, too_large)"""
    from app.services.topology import build_topology

    topo = await build_topology(session, user=user, include_vpn=False, include_vms=False, include_l3=False,
                                include_fdb=True, include_wireless=True, scope_limited=limited, subnet_ids=subnet_ids)
    if topo.get("too_large"):
        return {}, {}, topo["too_large"]
    allnodes = {n["data"]["id"]: n["data"] for n in topo["nodes"]
                if n["data"].get("kind") not in ("subnet", "vm", "vpn_site")}
    nodes = {k: v for k, v in allnodes.items() if v.get("type") in NETWORK_TYPES}
    adj: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in nodes}
    endpoints: dict[str, set[str]] = {k: set() for k in nodes}     # 網路設備 → 接在它下面的終端
    for e in topo["edges"]:
        d = e["data"]
        a, b = d.get("source"), d.get("target")
        if d.get("kind") not in DEVICE_EDGE_KINDS or a == b:
            continue
        if a in nodes and b in allnodes and b not in nodes:
            endpoints[a].add(b)
        if b in nodes and a in allnodes and a not in nodes:
            endpoints[b].add(a)
        if a not in nodes or b not in nodes:
            continue
        ev = d.get("evidence") or "inferred"
        # 埠：骨幹兩端各自的埠；存取層只知道交換器那一端的埠（兩邊都顯示它）；佈線沒有埠資料
        if d.get("kind") == "l2_uplink":
            ports = (d.get("port"), d.get("peer_port"))
        elif d.get("kind") == "l2":
            ports = (d.get("port"), d.get("port"))
        else:
            ports = (None, None)
        for x, y, port in ((a, b, ports[0]), (b, a, ports[1])):
            cur = adj[x].get(y)
            if cur is None or EV_RANK.get(ev, 0) > EV_RANK.get(cur["evidence"], 0):
                adj[x][y] = {"evidence": ev, "port": port or (cur or {}).get("port"), "kind": d.get("kind")}
    for k, eps in endpoints.items():
        nodes[k] = {**nodes[k], "endpoints": len(eps)}
    return nodes, adj, None


def _levels(nodes: dict[str, dict[str, Any]], adj: dict[str, dict[str, Any]]) -> dict[str, int]:
    """每台到它所在群組的根的距離。"""
    dist: dict[str, int] = {}
    order = sorted(nodes, key=lambda k: (TYPE_RANK.get(nodes[k].get("type") or "", 9), -len(adj[k]),
                                         nodes[k].get("label") or ""))
    for root in order:
        if root in dist:
            continue
        dist[root] = 0
        q = deque([root])
        while q:
            x = q.popleft()
            for y in adj[x]:
                if y not in dist:
                    dist[y] = dist[x] + 1
                    q.append(y)
    return dist


async def _anomalies_by_device(session: AsyncSession, ids: list[str]) -> dict[str, int]:
    """還沒消失的異常觀測，依 IP 對到裝置（IP 記錄連到的裝置）。"""
    import uuid

    from app.models.address import IPAddress
    from app.models.anomaly_observation import AnomalyObservation

    if not ids:
        return {}
    rows = (await session.execute(
        select(IPAddress.device_id, AnomalyObservation.id)
        .join(AnomalyObservation, AnomalyObservation.ip == IPAddress.ip)
        .where(AnomalyObservation.resolved_at.is_(None), actionable_observation(),
               in_values(IPAddress.device_id, [uuid.UUID(i) for i in ids])))).all()
    out: dict[str, set[Any]] = {}
    for did, oid in rows:
        out.setdefault(str(did), set()).add(oid)
    return {k: len(v) for k, v in out.items()}


async def _location_name(session: AsyncSession, loc_id: str | None) -> str | None:
    if not loc_id:
        return None
    import uuid

    from app.models.location import Location
    loc = await session.get(Location, uuid.UUID(loc_id))
    return loc.name if loc else None


def _brief(nodes: dict[str, dict[str, Any]], k: str) -> dict[str, Any]:
    n = nodes[k]
    return {"id": k, "name": n.get("label") or "", "type": n.get("type") or "other", "ip": n.get("ip")}


async def build(session: AsyncSession, *, user: Any, cfg: dict[str, Any], page: int = 1,
                now: datetime | None = None) -> dict[str, Any]:
    from app.mcp.tools import has_global_read

    now = now or datetime.now(UTC)
    from app.services.warroom.common import full_access
    admin = await full_access(session, user)
    base = {"key": "topology", "data_at": now.isoformat(), "per_page": 1}
    from app.services.warroom.common import scope_subnet_ids

    # 顯示範圍：只畫在範圍內子網路有位址的設備（與拓樸頁的子網路篩選同一套判斷）
    scope = await scope_subnet_ids(session, cfg)
    if scope is not None and not scope:
        return {**base, "page": 1, "pages": 1, "total": 0, "metrics": {"devices": 0, "links": 0, "inferred": 0},
                "focus": section("empty", devices=0)}
    nodes, adj, too_large = await _graph(session, user, not await has_global_read(session, user),
                                         list(scope) if scope is not None else None)
    if too_large:
        return {**base, "page": 1, "pages": 1, "total": 0, "metrics": {"devices": too_large["devices"],
                "links": None, "inferred": None}, "focus": section("too_large", **too_large)}
    links = {(min(a, b), max(a, b)): v["evidence"] for a in adj for b, v in adj[a].items()}
    metrics = {"devices": len(nodes), "links": len(links),
               "inferred": sum(1 for ev in links.values() if ev not in SOLID)}
    connected = [k for k in nodes if adj[k]]
    if not connected:
        return {**base, "page": 1, "pages": 1, "total": 0, "metrics": metrics,
                "focus": section("empty", devices=len(nodes))}
    anomalies = await _anomalies_by_device(session, connected) if admin else {}
    candidates = sorted(connected, key=lambda k: (-(anomalies.get(k, 0) > 0), -len(adj[k]),
                                                   nodes[k].get("label") or ""))[:MAX_FOCUS]
    pages = len(candidates)
    page = min(max(1, page), pages)
    f = candidates[page - 1]
    dist = _levels(nodes, adj)

    def neighbour(k: str) -> dict[str, Any]:
        link = adj[f][k]
        return {**_brief(nodes, k), "evidence": link["evidence"], "line": _line(link["evidence"]),
                "port": link.get("port"), "degree": len(adj[k]),
                "below": sum(1 for y in adj[k] if dist.get(y, 0) > dist.get(k, 0)),
                "endpoints": nodes[k].get("endpoints", 0),
                "anomalies": anomalies.get(k, 0) if admin else None}

    near = sorted(adj[f], key=lambda k: (-len(adj[k]), nodes[k].get("label") or ""))
    up = [k for k in near if dist.get(k, 0) < dist.get(f, 0)]
    down = [k for k in near if dist.get(k, 0) >= dist.get(f, 0)]
    ev_counts = dict.fromkeys(EV_RANK, 0)
    for v in adj[f].values():
        ev_counts[v["evidence"]] = ev_counts.get(v["evidence"], 0) + 1
    n = nodes[f]
    focus = {
        "device": {**_brief(nodes, f), "vendor": n.get("vendor"), "model": n.get("model"),
                   "location": await _location_name(session, n.get("location_id")), "degree": len(adj[f]),
                   "endpoints": n.get("endpoints", 0),
                   "anomalies": anomalies.get(f, 0) if admin else None},
        "up": [neighbour(k) for k in up[:MAX_UP]], "more_up": max(len(up) - MAX_UP, 0),
        "down": [{**neighbour(k), "peer": dist.get(k, 0) == dist.get(f, 0)} for k in down[:MAX_DOWN]],
        "more_down": max(len(down) - MAX_DOWN, 0),
        "evidence": ev_counts,
        "links": [neighbour(k) for k in near[:MAX_LINKS]], "more_links": max(len(near) - MAX_LINKS, 0),
    }
    return {**base, "page": page, "pages": pages, "total": pages, "metrics": metrics,
            "focus": section("ok", **focus)}
