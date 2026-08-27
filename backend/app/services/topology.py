"""網路拓樸圖：以 device + cabling + LibreNMS FDB（Phase 2 已有）拼出 graph。

回傳 Cytoscape.js 可直接吃的格式：
  {
    "nodes": [{"data": {"id": "...", "label": "...", "type": "..."}}, ...],
    "edges": [{"data": {"source": "...", "target": "...", "label": "..."}}, ...]
  }

邊（edges）來源：
1. 物理 Cable → 兩端 termination（同 cable 兩 termination → 一條邊）
2. WirelessLink（A/B device 都存在時）
3. VPNTunnel（A/B device）
4. L3 推導：device ↔ subnet（依 IP／名稱／ARP／LibreNMS 多訊號）
5. L2 推導：FDB → 存取層（機器 ↔ 交換器埠）與交換器骨幹

⚠️ 第 5 項在 v0.5.213 之前只存在於這段說明裡 —— 這個檔案宣稱用 FDB 拼圖，實際上一行
都沒讀。改動這裡時請一併確認說明與程式仍然對得上：說明會被當成事實引用。
LLDP/CDP（`librenms_links`）實機上是空的，沒有資料源，因此不列入。
"""

from __future__ import annotations

import ipaddress as _ipaddr
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import IPAddress
from app.models.advanced import WirelessLink
from app.models.device import Device
from app.models.librenms import ARPEntry, FDBEntry, LibreNMSDevice
from app.models.location import Location, Rack
from app.models.physical import Cable, CableTermination, VPNTunnel
from app.models.subnet import Subnet

#: 一個埠上出現幾個 MAC 就視為上行／trunk（而不是「這些機器都插在這個埠」）。
#: 存取埠通常是 1～2 個（機器本身 + 可能的一台 IP 話機）；上行埠動輒數十上百。
#: 實機上量到的分布很極端：28 個埠裡 12 個只有 1 個 MAC，13 個超過 4 個、最多 143 個
#: —— 中間幾乎沒有東西，所以這個門檻不敏感，訂在 4 很安全。
UPLINK_MAC_THRESHOLD = 4


async def build_topology(
    session: AsyncSession,
    *,
    user: Any = None,  # RBAC：限縮成該 user 可見的 device/subnet
    location_id: uuid.UUID | None = None,
    subnet_ids: list[uuid.UUID] | None = None,
    include_wireless: bool = True,
    include_vpn: bool = True,
    include_l3: bool = True,
    include_fdb: bool = True,
    online_only: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    # RBAC：可見的 device / subnet id（None = 全部可見，admin/wildcard）
    vis_dev: set[uuid.UUID] | None = None
    if user is not None and not getattr(user, "is_admin", False):
        from app.services.permission import visible_ids
        vis_dev = await visible_ids(session, user=user, object_type="device")

    # 若指定 subnet_ids：只保留「在這些子網路裡有 IP」的裝置，圖才不會被無關裝置塞爆
    subnet_filter = set(subnet_ids) if subnet_ids else None
    allowed_device_ids: set[str] | None = None
    if subnet_filter:
        # 篩選與「全部」一致：用三種訊號決定哪些裝置屬於這些子網路，
        # 否則只靠 IPAddress 連結會漏掉「以 IP 命名」或「ARP 看到」的裝置。
        fsubs = (await session.execute(
            select(Subnet).where(Subnet.id.in_(subnet_filter))
        )).scalars().all()
        fnets = []
        for sn in fsubs:
            try:
                fnets.append(_ipaddr.ip_network(str(sn.cidr), strict=False))
            except ValueError:
                continue
        allowed_device_ids = set()
        # (ip) 有 IPAddress 連結到這些子網路
        for d in (await session.execute(
            select(IPAddress.device_id).where(
                IPAddress.subnet_id.in_(subnet_filter),
                IPAddress.device_id.is_not(None),
            )
        )).all():
            if d[0] is not None:
                allowed_device_ids.add(str(d[0]))
        # (name) 裝置名稱即 IP 且落在這些子網路
        for did, dname in (await session.execute(select(Device.id, Device.name))).all():
            try:
                nip = _ipaddr.ip_address((dname or "").strip())
            except ValueError:
                continue
            if any(nip in n for n in fnets):
                allowed_device_ids.add(str(did))
        # (arp) 裝置的 ARP 鄰居 IP 落在這些子網路
        for did, ip in (await session.execute(
            select(ARPEntry.device_id, ARPEntry.ip).where(ARPEntry.device_id.is_not(None))
        )).all():
            try:
                aip = _ipaddr.ip_address(str(ip).split("/")[0])
            except ValueError:
                continue
            if any(aip in n for n in fnets):
                allowed_device_ids.add(str(did))

    # ── nodes：所有 device（選用依 location / subnet 過濾） ──
    dstmt = select(Device)
    if location_id is not None:
        dstmt = dstmt.where(Device.location_id == location_id)
    if allowed_device_ids is not None:
        dstmt = dstmt.where(Device.id.in_({uuid.UUID(x) for x in allowed_device_ids} or {uuid.UUID(int=0)}))
    if vis_dev is not None:
        dstmt = dstmt.where(Device.id.in_(vis_dev))
    devices = list((await session.execute(dstmt)).scalars().all())
    # 只畫上線：限縮成「至少有一個 IP 的 effective_status = online」的裝置
    if online_only:
        online_ids = {
            str(row[0]) for row in (await session.execute(
                select(IPAddress.device_id).where(
                    IPAddress.device_id.is_not(None),
                    IPAddress.effective_status == "online",
                ).distinct()
            )).all() if row[0]
        }
        devices = [d for d in devices if str(d.id) in online_ids]
    # 批次查每台裝置的主要 IP（給 node 帶上，連線卡片可顯示兩端 IP）
    pip_ids = {d.primary_ip_id for d in devices if d.primary_ip_id}  # type: ignore[attr-defined]
    pip_map: dict[uuid.UUID, str] = {}
    if pip_ids:
        for iid, ipval in (await session.execute(
            select(IPAddress.id, IPAddress.ip).where(IPAddress.id.in_(pip_ids))
        )).all():
            pip_map[iid] = str(ipval).split("/")[0]
    device_objs: dict[str, Device] = {}
    for d in devices:  # type: ignore[assignment]
        device_objs[str(d.id)] = d  # type: ignore[assignment]
        nodes[str(d.id)] = {
            "data": {
                "id": str(d.id),
                "label": d.name,
                "type": d.type,
                "vendor": d.vendor,
                "model": d.model,
                "serial": d.serial,
                "ip": pip_map.get(d.primary_ip_id) if d.primary_ip_id else None,
                "rack_id": str(d.rack_id) if d.rack_id else None,
                "location_id": str(d.location_id) if d.location_id else None,
            }
        }

    visible_device_ids = set(nodes.keys())

    # ── 物理纜線 ──
    cables = list((await session.execute(select(Cable))).scalars().all())
    for cable in cables:
        terms = list((await session.execute(
            select(CableTermination).where(CableTermination.cable_id == cable.id)
        )).scalars().all())
        if len(terms) != 2:
            continue
        a, b = sorted(terms, key=lambda t: t.side)
        # MVP：device-to-device 才畫
        if a.object_type != "device" or b.object_type != "device":
            continue
        sid, tid = str(a.object_id), str(b.object_id)
        if sid not in visible_device_ids or tid not in visible_device_ids:
            continue
        edges.append({
            "data": {
                "id": f"cable:{cable.id}",
                "source": sid, "target": tid,
                "label": cable.label or cable.type or "cable",
                "kind": "cable",
                "type": cable.type,
                "color": cable.color,
                "status": cable.status,
                "source_port": a.port_label,
                "target_port": b.port_label,
            }
        })

    # ── 無線連線 ──
    if include_wireless:
        wlinks = list((await session.execute(select(WirelessLink))).scalars().all())
        for w in wlinks:
            if not (w.a_device_id and w.b_device_id):
                continue
            sid, tid = str(w.a_device_id), str(w.b_device_id)
            if sid not in visible_device_ids or tid not in visible_device_ids:
                continue
            edges.append({
                "data": {
                    "id": f"wireless:{w.id}",
                    "source": sid, "target": tid,
                    "label": w.ssid or w.name,
                    "kind": "wireless",
                    "ssid": w.ssid,
                    "distance_m": w.distance_m,
                }
            })

    # ── VPN 邏輯連線 ──
    if include_vpn:
        tunnels = list((await session.execute(select(VPNTunnel))).scalars().all())

        # 對接的 VPN（兩端都是已知 device）一律要顯示並連起來——即使某端被子網路
        # 過濾掉、或該防火牆的管理 IP 沒掛進該網段。先把缺的端點 device 節點補進來。
        pair_dev_ids = {
            d for t in tunnels if t.a_device_id and t.b_device_id
            for d in (t.a_device_id, t.b_device_id)
        }
        missing = [d for d in pair_dev_ids if str(d) not in nodes]
        # RBAC：只補「可見」的對接端點裝置，非管理員不得藉 VPN 看到無權裝置/子網路
        if vis_dev is not None:
            missing = [d for d in missing if d in vis_dev]
        if missing:
            extra = (await session.execute(select(Device).where(Device.id.in_(missing)))).scalars().all()
            for d in extra:  # type: ignore[assignment]
                device_objs[str(d.id)] = d  # type: ignore[assignment]
                nodes[str(d.id)] = {"data": {
                    "id": str(d.id), "label": d.name, "type": d.type,
                    "vendor": d.vendor, "model": d.model, "serial": d.serial,
                    "rack_id": str(d.rack_id) if d.rack_id else None,
                    "location_id": str(d.location_id) if d.location_id else None,
                }}
                visible_device_ids.add(str(d.id))

        seen_vpn_pairs: set[frozenset[str]] = set()
        for t in tunnels:
            a_id = str(t.a_device_id) if t.a_device_id else None
            b_id = str(t.b_device_id) if t.b_device_id else None
            a_vis = bool(a_id) and a_id in visible_device_ids
            b_vis = bool(b_id) and b_id in visible_device_ids

            # 1) 兩端都是已知 device 且都可見 → device↔device 邊
            if a_vis and b_vis:
                # 對接的兩端各有一條 tunnel（A→B、B→A），同一對 device 只畫一條邊
                pair = frozenset((a_id, b_id))
                if pair in seen_vpn_pairs:
                    continue
                seen_vpn_pairs.add(pair)  # type: ignore[arg-type]
                # 對接邊上標出「中間經過的 WAN 端點」：a_endpoint ↔ b_endpoint
                # （WireGuard/IPsec/OpenVPN 的對外公網 IP / FQDN）
                wan = " ↔ ".join(x for x in (t.a_endpoint, t.b_endpoint) if x)
                edges.append({"data": {
                    "id": f"vpn:{t.id}", "source": a_id, "target": b_id,
                    "label": f"{t.type} · {wan}" if wan else t.type,
                    "kind": "vpn", "type": t.type, "status": t.status,
                    "a_endpoint": t.a_endpoint, "b_endpoint": t.b_endpoint,
                }})
                continue

            # 2) 只有一端可見（對端是外部站點，或對端 device 被過濾掉）→ 畫成遠端站點節點
            local = a_id if a_vis else (b_id if b_vis else None)
            if local is None:
                continue
            remote_label = t.b_endpoint or (t.name.split("/")[-1])
            site_id = f"vpnsite:{t.id}"
            nodes[site_id] = {"data": {
                "id": site_id,
                "label": remote_label,
                "type": "vpn_site",
                "kind": "vpn_site",
                "endpoint": t.b_endpoint,
                "tunnel": t.name,
                "vpn_type": t.type,
            }}
            edges.append({"data": {
                "id": f"vpn:{t.id}", "source": local, "target": site_id,
                "label": t.type, "kind": "vpn", "type": t.type, "status": t.status,
            }})

    # ── L3 子網路自動拓樸：多訊號推導 device↔subnet 鄰接（精確，不亂猜） ──
    #   (ip)   IPAddress.device_id 明確連結，且 IP 落在子網路
    #   (name) 裝置名稱本身就是某 IP（防火牆/路由器常以管理 IP 命名）落在子網路
    #   (arp)  該裝置的 ARP 紀錄（device_id 看到的鄰居 IP）落在子網路 → 它有介面在該網段
    # 每條邊標 via=來源，前端可顯示「依 IP / 名稱 / ARP 推得」。
    if include_l3:
        subnets_all = list((await session.execute(select(Subnet))).scalars().all())
        cand = []  # [(Subnet, ip_network)]
        for sn in subnets_all:
            if subnet_filter is not None and sn.id not in subnet_filter:
                continue
            try:
                cand.append((sn, _ipaddr.ip_network(str(sn.cidr), strict=False)))
            except ValueError:
                continue
        cand_sub_ids = {str(sn.id) for sn, _ in cand}

        # (device_id, subnet_id) → {"ip": 範例IP, "via": set()}
        assoc: dict[tuple[str, str], dict[str, Any]] = {}

        def _add(d_str: str, s_str: str, sample_ip: str | None, via: str) -> None:
            rec = assoc.get((d_str, s_str))
            if rec is None:
                rec = {"ip": sample_ip, "via": set()}
                assoc[(d_str, s_str)] = rec
            rec["via"].add(via)
            if sample_ip and not rec["ip"]:
                rec["ip"] = sample_ip

        # (ip) 明確連結的 IP
        ip_rows = (await session.execute(
            select(IPAddress.subnet_id, IPAddress.device_id, IPAddress.ip)
            .where(IPAddress.device_id.is_not(None))
        )).all()
        for sub_id, dev_id, ip in ip_rows:
            if sub_id is None or dev_id is None:
                continue
            s_str, d_str = str(sub_id), str(dev_id)
            if s_str not in cand_sub_ids or d_str not in visible_device_ids:
                continue
            _add(d_str, s_str, str(ip).split("/")[0], "ip")

        # (name) 裝置名稱即 IP，落在候選子網路（即使該 IP 未連結 device_id）
        for d_str in visible_device_ids:
            dev = device_objs.get(d_str)
            if dev is None:
                continue
            try:
                name_ip = _ipaddr.ip_address((dev.name or "").strip())
            except ValueError:
                continue
            for sn, net in cand:
                if name_ip in net:
                    _add(d_str, str(sn.id), str(name_ip), "name")

        # (arp) 裝置的 ARP 紀錄落在候選子網路 → 它接在該網段
        arp_rows = (await session.execute(
            select(ARPEntry.device_id, ARPEntry.ip).where(ARPEntry.device_id.is_not(None))
        )).all()
        for dev_id, ip in arp_rows:
            d_str = str(dev_id)
            if d_str not in visible_device_ids:
                continue
            try:
                a = _ipaddr.ip_address(str(ip).split("/")[0])  # type: ignore[assignment]
            except ValueError:
                continue
            for sn, net in cand:
                if a in net:
                    _add(d_str, str(sn.id), None, "arp")
                    break

        # (librenms) 用 LibreNMS 已知的管理 IP（primary_ip / hostname）關聯 device→subnet。
        #   交換器 / AP / 伺服器常沒有 IPAddress 連結、名稱也不是 IP，但 LibreNMS 知道
        #   它的管理 IP。這就是把 L2 裝置「掛進它所屬子網路」的訊號，否則它們會變成
        #   孤立節點，被前端藏掉（switch-003 / ap-001 不顯示就是這個原因）。
        #   注意：jt_ipam_device_id 可能指向「以 IP 命名」的重複裝置，而使用者看到的
        #   節點是「友善名稱」那一筆（同名重複）。所以除了用 jt_ipam_device_id 連，
        #   也用 sysname / hostname 去 match 同名的可見裝置節點，兩邊都連到子網路，
        #   重複裝置在收斂前也不會有人「不見」。
        name_to_dev: dict[str, str] = {}
        for _ds, _dev in device_objs.items():
            nm = (_dev.name or "").strip().lower()
            if nm:
                name_to_dev.setdefault(nm, _ds)
        ln_ip_rows = (await session.execute(
            select(LibreNMSDevice.jt_ipam_device_id, LibreNMSDevice.primary_ip,
                   LibreNMSDevice.hostname, LibreNMSDevice.sysname)
        )).all()
        for dev_id, pip, host, sysname in ln_ip_rows:
            mgmt = None
            for cand_ip in (pip, host):
                if not cand_ip:
                    continue
                try:
                    mgmt = _ipaddr.ip_address(str(cand_ip).split("/")[0].strip())
                    break
                except ValueError:
                    continue
            if mgmt is None:
                continue
            sub_id = None
            for sn, net in cand:
                if mgmt in net:
                    sub_id = str(sn.id)
                    break
            if sub_id is None:
                continue
            # 解析出所有對得上的可見裝置節點：直接連結 + 同名（sysname/hostname）
            targets: set[str] = set()
            if dev_id is not None and str(dev_id) in visible_device_ids:
                targets.add(str(dev_id))
            for nm in (sysname, host):
                key = (str(nm).strip().lower()) if nm else ""
                if key and key in name_to_dev:
                    targets.add(name_to_dev[key])
            for d_str in targets:
                _add(d_str, sub_id, str(mgmt), "librenms")

        # subnet nodes（只建有被關聯到的）
        used_subnet_ids = {s_str for (_, s_str) in assoc}
        if used_subnet_ids:
            for sn in subnets_all:
                sn_id = str(sn.id)
                if sn_id not in used_subnet_ids:
                    continue
                nodes[f"subnet:{sn_id}"] = {
                    "data": {
                        "id": f"subnet:{sn_id}",
                        "label": str(sn.cidr),
                        "type": "subnet",
                        "kind": "subnet",
                        "description": sn.description,
                        "subnet_uuid": sn_id,
                    }
                }

        # device → subnet edges
        for (d_str, s_str), rec in assoc.items():
            edges.append({
                "data": {
                    "id": f"l3:{d_str}:{s_str}",
                    "source": d_str,
                    "target": f"subnet:{s_str}",
                    "label": rec["ip"] or "",
                    "kind": "l3",
                    "via": ",".join(sorted(rec["via"])),
                }
            })

    # ── L2 存取層：FDB（哪個 MAC 出現在哪台交換器的哪個埠）──
    #
    # FDB 是目前唯一能自動畫出「東西掛在哪台交換器」與「交換器之間怎麼接」的來源
    # （LLDP/CDP 那張表在實機上是空的，沒有資料源）。兩個古典陷阱寫在
    # tests/test_topology_fdb.py：上行埠不是端點、同 MAC 對到多台裝置就不猜。
    #
    # FDB 屬證據契約的 learned 層：它說「曾經學到這個對應」，不說「現在活著」，
    # 所以這裡只畫線、不碰任何上線判定。
    if include_fdb:
        from app.services.arp_precedence import normalize_mac

        # ⚠️ `fdb_entries.device_id` 指的是 **LibreNMS 的裝置**，不是 jt-ipam 的 Device。
        # 直接在 SQL 裡接起來：一來沒連結到 Device 的交換器根本沒有節點可連，
        # 二來 FDB 是整個資料庫最會長的表之一（一台交換器就能有上萬列），
        # 撈回來再用 Python 篩會白扛一大堆用不到的列。
        fdb_rows = (await session.execute(
            select(FDBEntry.port_name, FDBEntry.mac, FDBEntry.vlan_id_num,
                   LibreNMSDevice.jt_ipam_device_id)
            .join(LibreNMSDevice, LibreNMSDevice.id == FDBEntry.device_id)
            .where(FDBEntry.port_name.is_not(None),
                   LibreNMSDevice.jt_ipam_device_id.is_not(None))
        )).all()

        if fdb_rows:
            # MAC → 裝置。對到多台就標成不明確（重疊網段下同一個 MAC 會有多筆 IP 記錄）。
            mac_to_dev: dict[str, str | None] = {}
            for dev_id, mac in (await session.execute(
                select(IPAddress.device_id, IPAddress.mac).where(
                    IPAddress.device_id.is_not(None), IPAddress.mac.is_not(None)
                )
            )).all():
                key = normalize_mac(mac)
                if not key:
                    continue
                d_str = str(dev_id)
                if key in mac_to_dev and mac_to_dev[key] != d_str:
                    mac_to_dev[key] = None       # 不明確 → 之後一律跳過
                else:
                    mac_to_dev.setdefault(key, d_str)

            # (交換器, 埠) → MAC 集合
            by_port: dict[tuple[str, str], set[str]] = {}
            vlan_of: dict[tuple[str, str, str], int | None] = {}
            for port_name, mac, vlan_num, sw_dev_id in fdb_rows:
                key = normalize_mac(mac)
                if not key:
                    continue
                pk = (str(sw_dev_id), str(port_name))
                by_port.setdefault(pk, set()).add(key)
                vlan_of.setdefault((*pk, key), vlan_num)

            switch_ids = {sw for sw, _ in by_port}
            # 每台交換器自己的 MAC（用來認出「對面那台就是它」）
            own_macs: dict[str, set[str]] = {}
            for mac, dev in mac_to_dev.items():
                if dev is not None:
                    own_macs.setdefault(dev, set()).add(mac)

            # ① 存取層：MAC 數不超過門檻的埠
            #
            #    ⚠️ 只有「這個埠上就這麼一個 MAC」才證明直接插在上面（`direct=True`）。
            #    埠上有兩三個 MAC 時，可能是底下接了一台笨集線器／小交換器，也可能是
            #    一台跑著多個虛擬機的主機 —— 那些機器確實在這個埠後面，但不見得是直連。
            #    實機上這個差別是看得到的：某台交換器的一個埠後面掛著三台不同的機器，
            #    而同一批機器又出現在另一台交換器的另一個埠上。兩邊都畫成直連就是說謊，
            #    所以邊上帶 `direct`，前端據此畫實線或虛線。
            l2_seen: set[tuple[str, str]] = set()
            adjacency: set[tuple[str, str]] = set()   # 已用單一 MAC 埠證實直連的交換器對
            for (sw_id, port), macs in sorted(by_port.items()):
                if sw_id not in visible_device_ids or len(macs) > UPLINK_MAC_THRESHOLD:
                    continue
                direct = len(macs) == 1
                for mac in sorted(macs):
                    peer = mac_to_dev.get(mac)
                    if peer is None or peer == sw_id or peer not in visible_device_ids:
                        continue                       # 不明確／自己／不在圖上
                    if (peer, sw_id) in l2_seen:
                        continue
                    l2_seen.add((peer, sw_id))
                    if peer in switch_ids and direct:
                        adjacency.add((min(sw_id, peer), max(sw_id, peer)))
                    edges.append({"data": {
                        "id": f"l2:{peer}:{sw_id}:{port}",
                        "source": peer, "target": sw_id,
                        "label": port, "kind": "l2", "via": "fdb",
                        "port": port, "direct": direct,
                        "port_mac_count": len(macs),
                        "vlan": vlan_of.get((sw_id, port, mac)),
                    }})

            # ② 骨幹：兩台交換器直連的判準（Breitbart 等人的 FDB 拓樸推導條件）
            #
            #    A 的某個埠 P 上看得到 B 自己的 MAC，且 B 的某個埠 Q 上看得到 A 自己的
            #    MAC，**而且 P 與 Q 背後的 MAC 集合不相交**。
            #
            #    第三個條件才是關鍵：少了它，A—B—C 這種串接會被畫成 A—C 也直連
            #    （C 的 MAC 當然會出現在 A 朝 B 的那個埠上）。相交檢查會擋掉，因為
            #    A 朝 B 的埠與 C 朝 B 的埠都含有 B 與 B 底下那些機器。
            #    單邊只看得到對方（對面那台沒在回報 FDB）時**不畫** —— 無從證實。
            uplink_pairs: dict[tuple[str, str], tuple[str, str]] = {}
            for a in sorted(switch_ids):
                for b in sorted(switch_ids):
                    if a >= b:
                        continue
                    pair = (a, b)
                    if pair in adjacency or pair in uplink_pairs:
                        continue
                    if a not in visible_device_ids or b not in visible_device_ids:
                        continue
                    a_ports = [(p, m) for (s, p), m in by_port.items()
                               if s == a and m & own_macs.get(b, set())]
                    b_ports = [(p, m) for (s, p), m in by_port.items()
                               if s == b and m & own_macs.get(a, set())]
                    for pa, ma in a_ports:
                        for pb, mb in b_ports:
                            # 兩個埠背後的機器不可以重疊；對方交換器自己的 MAC 不算
                            if (ma - own_macs.get(b, set())) & (mb - own_macs.get(a, set())):
                                continue
                            uplink_pairs[pair] = (pa, pb)
                            break
                        if pair in uplink_pairs:
                            break

            for (a, b), (pa, pb) in sorted(uplink_pairs.items()):
                edges.append({"data": {
                    "id": f"l2u:{a}:{b}",
                    "source": a, "target": b,
                    # port 對應 source 那端、peer_port 對應 target 那端（source/target
                    # 由 id 排序決定，兩端誰先誰後沒有意義）
                    "label": f"{pa} ↔ {pb}", "kind": "l2_uplink", "via": "fdb",
                    "port": pa, "peer_port": pb,
                }})

    # ── 節點細節加值：rack/location 名稱、管理 IP、LibreNMS 撈回的 os/hardware/版本/狀態 ──
    if device_objs:
        dev_uuids = [d.id for d in device_objs.values()]
        rack_ids = {d.rack_id for d in device_objs.values() if d.rack_id}
        loc_ids = {d.location_id for d in device_objs.values() if d.location_id}
        pip_ids = {d.primary_ip_id for d in device_objs.values() if d.primary_ip_id}

        rack_names: dict[str, str] = {}
        if rack_ids:
            for r in (await session.execute(select(Rack).where(Rack.id.in_(rack_ids)))).scalars().all():
                rack_names[str(r.id)] = r.name
        loc_names: dict[str, str] = {}
        if loc_ids:
            for lo in (await session.execute(select(Location).where(Location.id.in_(loc_ids)))).scalars().all():
                loc_names[str(lo.id)] = lo.name
        pip_map: dict[str, str] = {}
        if pip_ids:
            for pid, pip in (await session.execute(
                select(IPAddress.id, IPAddress.ip).where(IPAddress.id.in_(pip_ids))
            )).all():
                pip_map[str(pid)] = str(pip).split("/")[0]
        ln_map: dict[str, LibreNMSDevice] = {}
        ln_rows = (await session.execute(
            select(LibreNMSDevice).where(LibreNMSDevice.jt_ipam_device_id.in_(dev_uuids))
        )).scalars().all()
        for ln in ln_rows:
            ln_map[str(ln.jt_ipam_device_id)] = ln

        for d_str, dev in device_objs.items():
            data = nodes[d_str]["data"]
            # 管理 IP：primary_ip → 否則 device 名稱本身若是 IP
            ip = pip_map.get(str(dev.primary_ip_id)) if dev.primary_ip_id else None
            if not ip:
                try:
                    ip = str(_ipaddr.ip_address((dev.name or "").strip()))
                except ValueError:
                    ip = None
            if ip:
                data["ip"] = ip
            if dev.rack_id and str(dev.rack_id) in rack_names:
                data["rack"] = rack_names[str(dev.rack_id)]
            if dev.location_id and str(dev.location_id) in loc_names:
                data["location"] = loc_names[str(dev.location_id)]
            ln = ln_map.get(d_str)  # type: ignore[assignment]
            if ln is not None:
                # Device.type 多半是 "other"（LibreNMS 早期 sync 沒細分）；用 os/hardware
                # 重新推一次，讓 AP/交換器/伺服器在圖例與顏色上分得出來。
                if data.get("type") in (None, "other"):
                    from app.services.librenms import _infer_device_type
                    refined = _infer_device_type(ln)
                    if refined != "other":
                        data["type"] = refined
                if ln.os:
                    data["os"] = ln.os
                if ln.hardware:
                    data["hardware"] = ln.hardware
                if ln.version:
                    data["sw_version"] = ln.version
                if ln.sysname:
                    data["sysname"] = ln.sysname
                if ln.status:
                    data["status"] = ln.status

    return {"nodes": list(nodes.values()), "edges": edges}
