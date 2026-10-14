"""上線狀態跟著所屬設備（客戶 2026-10-14：多 LAN 路由器的閘道不回 ping 卻在用，登記後顯示紅燈）。

勾了 liveness_from_device 的 IP：每輪把同一台設備其他位址最新的**會過期**證據搬到 arp_seen["device"]，
時間是原本那筆證據的時間 → 設備掛了證據一起過期、一起變紅（不可以永遠綠）。
沒掛設備時不算離線（「使用中（不回應探測）」，灰色）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.address import IPAddress
from app.models.device import Device
from app.models.section import Section
from app.models.subnet import Subnet
from app.services import device_liveness as dl

pytestmark = pytest.mark.anyio


async def _router(db, *, sibling_scanner=None, sibling_arp_seen=None):  # type: ignore[no-untyped-def]
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    lan1 = Subnet(section_id=sec.id, cidr="192.0.2.0/24")
    lan2 = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    dev = Device(name=f"rtr-{uuid.uuid4().hex[:4]}", type="router")
    db.add_all([lan1, lan2, dev])
    await db.flush()
    main = IPAddress(subnet_id=lan1.id, ip="192.0.2.1", state="used", device_id=dev.id,
                     last_seen_scanner=sibling_scanner, arp_seen=sibling_arp_seen or {})
    gw = IPAddress(subnet_id=lan2.id, ip="198.51.100.1", state="used", device_id=dev.id, liveness_from_device=True)
    db.add_all([main, gw])
    await db.flush()
    return dev, main, gw


async def test_gateway_follows_the_routers_reachable_address(db_session) -> None:
    seen = datetime.now(UTC) - timedelta(minutes=3)
    _dev, _main, gw = await _router(db_session, sibling_scanner=seen)
    out = await dl.materialize(db_session)
    assert out["stamped"] >= 1
    assert datetime.fromisoformat(gw.arp_seen["device"]) == seen, "時間是原本那筆證據的時間，不是現在"
    assert str(gw.device_evidence_ip) == "192.0.2.1"


async def test_only_aging_evidence_is_carried_over(db_session) -> None:
    """DHCP 租約這種不會過期的證據不搬：搬了就等於設備掛了也永遠綠。"""
    lease = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    _dev, _main, gw = await _router(db_session, sibling_arp_seen={"lease:opnsense": lease})
    await dl.materialize(db_session)
    assert "device" not in (gw.arp_seen or {})
    assert gw.device_evidence_ip is None


async def test_librenms_device_status_counts(db_session) -> None:
    from app.models.librenms import LibreNMSDevice, LibreNMSInstance
    dev, _main, gw = await _router(db_session)
    inst = LibreNMSInstance(name=f"l-{uuid.uuid4().hex[:6]}", api_url="https://librenms.example",
                            api_token_enc=b"x", api_token_nonce=b"y")
    db_session.add(inst)
    await db_session.flush()
    polled = datetime.now(UTC) - timedelta(minutes=2)
    db_session.add(LibreNMSDevice(instance_id=inst.id, legacy_device_id=7, hostname="rtr", status="up",
                                  jt_ipam_device_id=dev.id, last_seen_at=polled))
    await db_session.flush()
    await dl.materialize(db_session)
    assert datetime.fromisoformat(gw.arp_seen["device"]) == polled
    assert gw.device_evidence_ip is None, "依據是 LibreNMS 的設備狀態，不是某個位址"


async def test_turning_the_flag_off_removes_the_evidence(db_session) -> None:
    _dev, _main, gw = await _router(db_session, sibling_scanner=datetime.now(UTC))
    await dl.materialize(db_session)
    assert "device" in gw.arp_seen
    gw.liveness_from_device = False
    await db_session.flush()
    await dl.materialize(db_session)
    assert "device" not in (gw.arp_seen or {})
    assert gw.device_evidence_ip is None


async def test_without_a_device_it_is_unknown_not_offline(db_session) -> None:
    """沒掛設備：不算離線（灰色「使用中（不回應探測）」），不是紅燈。"""
    from app.services.agent_scope import classify_liveness
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    db_session.add(sub)
    await db_session.flush()
    ip = IPAddress(subnet_id=sub.id, ip="203.0.113.1", state="used", liveness_from_device=True)
    db_session.add(ip)
    await db_session.flush()
    await dl.materialize(db_session)
    now = datetime.now(UTC)
    assert classify_liveness(ip, minutes=30, sources=["scanner"], now=now) == "unknown"
    ip.liveness_from_device = False
    assert classify_liveness(ip, minutes=30, sources=["scanner"], now=now) == "offline"


async def test_recompute_marks_the_gateway_online(db_session) -> None:
    from app.services.librenms import recompute_effective_status
    _dev, _main, gw = await _router(db_session, sibling_scanner=datetime.now(UTC) - timedelta(minutes=1))
    await db_session.commit()
    await recompute_effective_status(db_session)
    await db_session.refresh(gw)
    assert (gw.effective_status or "").startswith("online"), gw.effective_status


async def test_same_mac_lists_visible_siblings(client, auth_headers, db_session) -> None:
    """IP 詳細頁的提示：同 MAC 的另一個位址在線 → 多半是同一台路由器的介面。只列看得到的子網路。"""
    _dev, main, gw = await _router(db_session, sibling_scanner=datetime.now(UTC))
    main.mac = "02:00:5e:10:00:01"
    gw.mac = "02:00:5e:10:00:01"
    gw.liveness_from_device = False
    await db_session.commit()
    r = await client.get(f"/api/v1/addresses/{gw.id}/same-mac", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert [x["ip"].split("/")[0] for x in r.json()] == ["192.0.2.1"]
    assert r.json()[0]["device_id"] == str(main.device_id)


async def test_device_source_is_always_offered(client, auth_headers) -> None:
    r = await client.get("/api/v1/system/online-grace", headers=auth_headers)
    assert r.status_code == 200
    avail = {x["key"]: x for x in r.json()["available"]}
    assert avail["device"]["configured"] is True
