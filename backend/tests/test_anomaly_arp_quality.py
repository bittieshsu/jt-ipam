"""異常偵測的 ARP 資料品質（2026-10-09 一次 ARP 異常調查的回饋，位址與名稱已換成通用值）。

1. 路由器 ARP 表切換到一半被 SNMP 讀到 → 兩個拼接出來的假 MAC（只有那台回報、查不到廠商）。
   標成疑似讀壞、列出來但不算機器數，也不算「頻繁換 MAC」的種數。
2. 可信的 MAC 都屬於同一台裝置 → 不是 IP 衝突，歸類成 ARP flux，附上是哪張網卡與 sysctl 修法。
3. 兩個子網段在同一個二層 → l2_subnet_bleed。
4. MCP list_anomalies 指定單一 kind 時 items 是陣列。
5. 每個 MAC 都寫出是哪台設備的 ARP 表回報；單一來源的本地管理位址跟多來源佐證的 MAC 不一致 → 過期快取。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.librenms import ARPEntry, FDBEntry, LibreNMSDevice, LibreNMSInstance

from tests.test_ip_conflict_evidence import _device, _ip, _subnet

A = "02:00:5e:10:00:a1"        # 主機板網卡
B = "06:00:5e:20:00:b2"        # 擴充網卡
SPLICE_1 = "02:00:5e:20:00:b2"
SPLICE_2 = "06:00:5e:10:00:a1"


async def _ln(session, *names: str) -> dict[str, LibreNMSDevice]:
    inst = LibreNMSInstance(name=f"ln-{uuid.uuid4().hex[:6]}", api_url="https://ln.example.com",
                            api_token_enc=b"x", api_token_nonce=b"x")
    session.add(inst)
    await session.flush()
    out = {}
    for i, n in enumerate(names):
        d = LibreNMSDevice(instance_id=inst.id, legacy_device_id=1000 + i + uuid.uuid4().int % 100000,
                           hostname=f"192.0.2.{10 + i}", sysname=n)
        session.add(d)
        out[n] = d
    await session.flush()
    return out


def _arp(session, ip: str, mac: str, dev: LibreNMSDevice, *, iface: str = "igb0", minutes: int = 5) -> None:
    now = datetime.now(UTC)
    session.add(ARPEntry(ip=ip, mac=mac, instance_id=dev.instance_id, device_id=dev.id, interface=iface,
                         source="librenms", first_seen_at=now - timedelta(days=30),
                         last_seen_at=now - timedelta(minutes=minutes)))


async def _host_with_spliced_macs(db, ip: str, *, linked: bool):
    """雙網卡主機：A 有很多台設備看過、B 有兩台，拼接出來的兩個只有路由器看過。"""
    ln = await _ln(db, "edge-router", "core-sw", "access-sw")
    s1 = await _subnet(db, "198.51.100.0/24", anomaly_enabled=True)
    s2 = await _subnet(db, "203.0.113.0/24", anomaly_enabled=True)
    dev = await _device(db) if linked else None
    await _ip(db, s1, ip, mac=A, device_id=dev.id if dev else None)
    await _ip(db, s2, ip.replace("198.51.100", "203.0.113"), mac=B, device_id=dev.id if dev else None)
    for d in ln.values():
        _arp(db, ip, A, d)
    _arp(db, ip, B, ln["edge-router"])
    _arp(db, ip, B, ln["core-sw"])
    _arp(db, ip, SPLICE_1, ln["edge-router"])
    _arp(db, ip, SPLICE_2, ln["edge-router"])
    await db.flush()
    return ln, dev


async def test_spliced_macs_are_flagged_and_reporters_are_named(db_session) -> None:
    from app.services.anomaly import detect_ip_conflicts

    await _host_with_spliced_macs(db_session, "198.51.100.21", linked=False)
    (row,) = [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "198.51.100.21"]
    by_mac = {m["mac"]: m for m in row["macs"]}
    assert by_mac[SPLICE_1]["suspect"] == "corrupt" and by_mac[SPLICE_2]["suspect"] == "corrupt"
    assert by_mac[A]["suspect"] is None and by_mac[B]["suspect"] is None
    assert row["suspect_count"] == 2
    assert by_mac[SPLICE_1]["reporter_count"] == 1
    assert by_mac[SPLICE_1]["reporters"][0]["device"] == "edge-router"
    assert by_mac[SPLICE_1]["reporters"][0]["interface"] == "igb0"
    assert by_mac[A]["reporter_count"] == 3


async def test_one_real_mac_plus_a_misread_one_is_not_a_conflict(db_session) -> None:
    from app.services.anomaly import detect_ip_conflicts

    ln = await _ln(db_session, "edge-router", "core-sw")
    sub = await _subnet(db_session)
    await _ip(db_session, sub, "198.51.100.22", mac=A)
    _arp(db_session, "198.51.100.22", A, ln["edge-router"])
    _arp(db_session, "198.51.100.22", A, ln["core-sw"])
    _arp(db_session, "198.51.100.22", "02:00:5e:10:00:99", ln["edge-router"])   # 只差一個位元組
    await db_session.flush()
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "198.51.100.22"]


async def test_dual_nic_host_with_spliced_macs_is_arp_flux_not_a_conflict(db_session) -> None:
    from app.services.anomaly import detect_arp_flux, detect_ip_conflicts

    _ln_, dev = await _host_with_spliced_macs(db_session, "198.51.100.23", linked=True)
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "198.51.100.23"]
    (row,) = [r for r in await detect_arp_flux(db_session) if r["ip"] == "198.51.100.23"]
    assert row["host"] == dev.name
    by_mac = {m["mac"]: m for m in row["macs"]}
    assert by_mac[B]["nic"] == ["203.0.113.23"], "看得出這個 MAC 是哪張網卡（登記在哪個 IP）"
    assert by_mac[SPLICE_1]["suspect"] == "corrupt"
    assert "net.ipv4.conf.all.arp_ignore=1" in row["fix"]
    assert "net.ipv4.conf.all.arp_announce=2" in row["fix"]


async def test_existing_two_nic_case_now_shows_up_as_arp_flux(db_session) -> None:
    """2026-10-04 的雙網卡案例以前只是從衝突裡消失；現在歸到 ARP flux。"""
    from app.services.anomaly import detect_arp_flux
    from app.services.arp_evidence import record_arp_observation

    dev = await _device(db_session)
    s1 = await _subnet(db_session, "198.51.100.0/24")
    s2 = await _subnet(db_session, "203.0.113.0/24")
    a = await _ip(db_session, s1, "198.51.100.24", mac=A, device_id=dev.id)
    await _ip(db_session, s2, "203.0.113.24", mac=B, device_id=dev.id)
    await record_arp_observation(db_session, ip=a, mac=A, source="scanner")
    await record_arp_observation(db_session, ip=a, mac=B, source="arp:opnsense")
    assert [r for r in await detect_arp_flux(db_session) if r["ip"] == "198.51.100.24"]


async def test_stale_local_mac_from_one_switch_is_not_a_conflict(db_session) -> None:
    from app.services.anomaly import detect_ip_conflicts

    ln = await _ln(db_session, "edge-router", "core-sw", "access-sw", "bridge-sw")
    sub = await _subnet(db_session, "203.0.113.0/24")
    real, stale = "00:00:5e:00:53:10", "0e:00:5e:00:53:11"
    await _ip(db_session, sub, "203.0.113.42", mac=real)
    for n in ("edge-router", "core-sw", "access-sw"):
        _arp(db_session, "203.0.113.42", real, ln[n])
    _arp(db_session, "203.0.113.42", stale, ln["bridge-sw"], iface="bridge")
    await db_session.flush()
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "203.0.113.42"]


async def test_spliced_macs_do_not_count_towards_mac_flapping(db_session) -> None:
    from app.services.anomaly import detect_mac_flapping

    await _host_with_spliced_macs(db_session, "198.51.100.25", linked=False)
    assert not [r for r in await detect_mac_flapping(db_session) if r["ip"] == "198.51.100.25"], \
        "可信的只有兩個 MAC，不到四個"


async def test_mac_flapping_counts_real_macs_and_lists_the_suspect_ones(db_session) -> None:
    from app.services.anomaly import detect_mac_flapping

    ln = await _ln(db_session, "edge-router", "core-sw")
    sub = await _subnet(db_session, anomaly_enabled=True)
    await _ip(db_session, sub, "198.51.100.26")
    reals = [f"02:00:5e:00:53:{i:02x}" for i in (0x20, 0x31, 0x42, 0x53)]
    for m in reals:
        _arp(db_session, "198.51.100.26", m, ln["edge-router"])
        _arp(db_session, "198.51.100.26", m, ln["core-sw"])
    _arp(db_session, "198.51.100.26", "02:00:5e:00:53:21", ln["edge-router"])  # 跟 0x20 只差一個位元組
    await db_session.flush()
    (row,) = [r for r in await detect_mac_flapping(db_session) if r["ip"] == "198.51.100.26"]
    assert row["mac_count"] == 4 and row["suspect_count"] == 1
    assert {m["mac"]: m["suspect"] for m in row["macs"]}["02:00:5e:00:53:21"] == "corrupt"


async def test_two_subnets_on_one_l2_segment_are_reported(db_session) -> None:
    from app.services.anomaly import detect_l2_subnet_bleed
    from app.services.arp_evidence import record_arp_observation

    ln = await _ln(db_session, "lan-sw")
    s1 = await _subnet(db_session, "198.51.100.0/24", anomaly_enabled=True)
    s2 = await _subnet(db_session, "203.0.113.0/24", anomaly_enabled=True)
    a = await _ip(db_session, s1, "198.51.100.30", mac=A)
    b = await _ip(db_session, s2, "203.0.113.30", mac=B)
    await record_arp_observation(db_session, ip=a, mac=A, source="scanner")
    await record_arp_observation(db_session, ip=b, mac=B, source="scanner")
    # 198.51.100.30 的 ARP 回應用了 203.0.113.0/24 那張網卡的 MAC
    await record_arp_observation(db_session, ip=a, mac=B, source="arp:opnsense")
    # 路由器的子介面共用一個 MAC：登記在兩個子網段，不拿來判斷
    r1 = await _ip(db_session, s1, "198.51.100.1", mac="00:00:5e:00:53:fe")
    r2 = await _ip(db_session, s2, "203.0.113.1", mac="00:00:5e:00:53:fe")
    await record_arp_observation(db_session, ip=r1, mac="00:00:5e:00:53:fe", source="scanner")
    await record_arp_observation(db_session, ip=r2, mac="00:00:5e:00:53:fe", source="scanner")
    # 交換器的同一個 VLAN 學到兩個子網段的 MAC
    now = datetime.now(UTC)
    for mac, port in ((A, "Gi1/0/5"), (B, "Gi1/0/25")):
        db_session.add(FDBEntry(mac=mac, vlan_id_num=1, instance_id=ln["lan-sw"].instance_id,
                                device_id=ln["lan-sw"].id, port_name=port, source="librenms",
                                first_seen_at=now - timedelta(days=2), last_seen_at=now))
    await db_session.flush()

    rows = [r for r in await detect_l2_subnet_bleed(db_session)
            if set(r["subnets"]) == {"198.51.100.0/24", "203.0.113.0/24"}]
    assert len(rows) == 1
    row = rows[0]
    assert set(row["evidence"]) == {"arp_answer", "switch_fdb"}
    assert row["arp_examples"][0]["ip"] == "198.51.100.30"
    assert row["arp_examples"][0]["mac_owner_ip"] == "203.0.113.30"
    assert all(x["mac"] != "00:00:5e:00:53:fe" for x in row["arp_examples"])
    assert row["fdb_examples"][0]["switch"] == "lan-sw" and row["fdb_examples"][0]["vlan"] == 1


async def test_list_anomalies_with_one_kind_returns_an_array(db_session, admin_user) -> None:
    from app.mcp.tools import list_anomalies

    admin = admin_user
    out = await list_anomalies(db_session, admin, kind="arp_flux")
    assert out["kind"] == "arp_flux" and isinstance(out["items"], list)
    out = await list_anomalies(db_session, admin)
    assert isinstance(out["items"], dict) and "l2_subnet_bleed" in out["items"]


async def test_ip_history_groups_arp_by_mac_with_reporters(db_session, admin_user) -> None:
    from app.mcp.tools import get_ip_history

    await _host_with_spliced_macs(db_session, "198.51.100.27", linked=False)
    admin = admin_user
    out = await get_ip_history(db_session, user=admin, ip="198.51.100.27")
    arp = {e["mac"]: e for e in out["events"] if e["kind"] == "arp"}
    assert set(arp) == {A, B, SPLICE_1, SPLICE_2}
    assert arp[SPLICE_1]["suspect"] == "corrupt" and arp[SPLICE_1]["reporter_count"] == 1
    assert arp[SPLICE_1]["reporters"][0]["device"] == "edge-router"
    assert arp[A]["reporter_count"] == 3


async def test_misread_mac_is_caught_after_the_other_nic_stopped_answering(db_session) -> None:
    """修好 arp_ignore 之後：最近一小時只剩真實 MAC 與路由器還在回報的拼接 MAC → 不是衝突。"""
    from app.services.anomaly import detect_arp_flux, detect_ip_conflicts

    ln = await _ln(db_session, "edge-router", "core-sw")
    dev = await _device(db_session)
    s1 = await _subnet(db_session, "198.51.100.0/24")
    s2 = await _subnet(db_session, "203.0.113.0/24")
    await _ip(db_session, s1, "198.51.100.28", mac=A, device_id=dev.id)
    await _ip(db_session, s2, "203.0.113.28", mac=B, device_id=dev.id)
    _arp(db_session, "198.51.100.28", A, ln["edge-router"])
    _arp(db_session, "198.51.100.28", A, ln["core-sw"])
    _arp(db_session, "198.51.100.28", B, ln["edge-router"], minutes=60 * 5)     # 五小時前就不再回應
    _arp(db_session, "198.51.100.28", SPLICE_1, ln["edge-router"])
    await db_session.flush()
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "198.51.100.28"]
    assert not [r for r in await detect_arp_flux(db_session) if r["ip"] == "198.51.100.28"], \
        "可信的只剩一個 MAC：已經不是 ARP flux 了"
