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


def _arp(session, ip: str, mac: str, dev: LibreNMSDevice, *, iface: str = "igb0", minutes: int = 5,
         first_minutes: int = 30 * 24 * 60) -> None:
    now = datetime.now(UTC)
    session.add(ARPEntry(ip=ip, mac=mac, instance_id=dev.instance_id, device_id=dev.id, interface=iface,
                         source="librenms", first_seen_at=now - timedelta(minutes=first_minutes),
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
    # 兩個來源都看到同一筆：例子只列一次（正式環境 2026-10-20 畫面上同一行出現兩次）
    await record_arp_observation(db_session, ip=a, mac=B, source="scanner")
    _arp(db_session, "198.51.100.30", B, ln["lan-sw"])      # LibreNMS 的觀測沒有子網路
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
    assert row["arp_count"] == 1, row["arp_examples"]
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


async def test_two_nics_of_one_vm_are_arp_flux_even_without_a_device(db_session) -> None:
    """正式環境 2026-10-20：一台 Proxmox VM 兩張網卡（net0 在 192.168、net1 在 172.16），IP 沒有連到裝置，
    掃描代理看到 net0 回答 net1 的位址 → 被報成「IP 衝突」。虛擬化整合明明知道兩張網卡是同一台 VM。"""
    from app.models.virt import VirtCluster, VirtualMachine, VMInterface
    from app.services.anomaly import detect_arp_flux, detect_ip_conflicts
    from app.services.arp_evidence import record_arp_observation

    cl = VirtCluster(name=f"cl-{uuid.uuid4().hex[:6]}")
    db_session.add(cl)
    await db_session.flush()
    vm = VirtualMachine(cluster_id=cl.id, name="app-dev-01")
    db_session.add(vm)
    await db_session.flush()
    db_session.add_all([VMInterface(vm_id=vm.id, name="net0", mac=A), VMInterface(vm_id=vm.id, name="net1", mac=B)])
    s1 = await _subnet(db_session, "198.51.100.0/24")
    s2 = await _subnet(db_session, "203.0.113.0/24")
    a = await _ip(db_session, s1, "198.51.100.29", mac=B)          # 這個位址設在 net1
    await _ip(db_session, s2, "203.0.113.29", mac=A)
    await record_arp_observation(db_session, ip=a, mac=B, source="arp:opnsense")
    await record_arp_observation(db_session, ip=a, mac=A, source="scanner")   # net0 也回答
    await db_session.flush()
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "198.51.100.29"]
    (row,) = [r for r in await detect_arp_flux(db_session) if r["ip"] == "198.51.100.29"]
    assert row["host"] == "app-dev-01"
    assert {m["mac"]: m["nic"] for m in row["macs"]}[A][0] == "net0"


async def test_the_other_nic_being_online_elsewhere_is_still_arp_flux(db_session) -> None:
    """「過期快取」規則不可吃掉 ARP flux：flux 的另一個 MAC 本來就登記在同一台的另一個（上線的）IP 上
    （正式環境 2026-10-20 部署後幾台雙網卡 VM 一度從 ARP flux 消失）。"""
    from app.models.virt import VirtCluster, VirtualMachine, VMInterface
    from app.services.anomaly import detect_arp_flux, detect_ip_conflicts
    from app.services.arp_evidence import record_arp_observation

    n0, n1 = "bc:24:11:31:41:51", "bc:24:11:c2:d3:e4"   # Proxmox 全域位址（正式環境的情形）
    cl = VirtCluster(name=f"cl-{uuid.uuid4().hex[:6]}")
    db_session.add(cl)
    await db_session.flush()
    vm = VirtualMachine(cluster_id=cl.id, name="app-dev-02")
    db_session.add(vm)
    await db_session.flush()
    db_session.add_all([VMInterface(vm_id=vm.id, name="net0", mac=n0), VMInterface(vm_id=vm.id, name="net1", mac=n1)])
    s1 = await _subnet(db_session, "198.51.100.0/24")
    s2 = await _subnet(db_session, "203.0.113.0/24")
    a = await _ip(db_session, s1, "198.51.100.39", mac=n1)
    other = await _ip(db_session, s2, "203.0.113.39", mac=n0)
    other.effective_status = "online"
    await record_arp_observation(db_session, ip=a, mac=n1, source="arp:opnsense")
    await record_arp_observation(db_session, ip=a, mac=n1, source="scanner")
    await record_arp_observation(db_session, ip=a, mac=n0, source="arp:fortigate")
    await db_session.flush()
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "198.51.100.39"]
    assert [r for r in await detect_arp_flux(db_session) if r["ip"] == "198.51.100.39"]


async def test_each_hosts_own_docker_network_is_not_a_conflict(db_session) -> None:
    """正式環境 2026-10-20：172.17.0.2 被三台主機各自的 ARP 表回報、三個不同 MAC —— 那是三台主機各自的
    docker0 網段，剛好用同一段預設位址，不是同一個網路。IPAM 沒有這個網段、每個 MAC 只有一台回報、回報者不重疊。"""
    from app.services.anomaly import detect_ip_conflicts
    ln = await _ln(db_session, "host-a", "host-b", "host-c")
    await _subnet(db_session, "172.31.250.0/24", anomaly_enabled=True)   # 位址在範圍內，才驗得到這條規則
    for i, d in enumerate(ln.values()):
        _arp(db_session, "172.31.250.2", f"02:42:ac:11:00:0{i + 2}", d, iface="docker0")
    await db_session.flush()
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "172.31.250.2"]


async def test_a_single_stale_entry_for_a_mac_that_lives_elsewhere_is_not_a_conflict(db_session) -> None:
    """正式環境 2026-10-20：只有一台主機的 ARP 表還留著「.173 → 另一台 VM 的 MAC」（那台 VM 現在在 .154），
    .173 真正的主人有十幾個來源佐證。單一來源、而且 IPAM 知道那個 MAC 在別的位址 → 過期快取。"""
    from app.services.anomaly import detect_ip_conflicts
    ln = await _ln(db_session, "router", "sw-1", "sw-2", "stale-host")
    s = await _subnet(db_session, "198.51.100.0/24", anomaly_enabled=True)
    p, q = "bc:24:11:10:20:30", "bc:24:11:a0:b0:c0"     # 全域位址（Proxmox OUI）、不只差一個位元組
    await _ip(db_session, s, "198.51.100.73", mac=p)
    other = await _ip(db_session, s, "198.51.100.54", mac=q)
    other.effective_status = "online"
    for n in ("router", "sw-1", "sw-2"):
        _arp(db_session, "198.51.100.73", p, ln[n])
    _arp(db_session, "198.51.100.73", q, ln["stale-host"])
    await db_session.flush()
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "198.51.100.73"]


async def test_a_container_recreated_with_a_new_mac_is_not_a_conflict(db_session) -> None:
    """正式環境 2026-10-20：一台開發主機每幾小時重建容器，容器位址每次換一個隨機 MAC；只有那台主機自己回報，
    前一個 MAC 最後一次出現之後下一個才出現 —— 是換掉，不是兩台同時在搶。"""
    from app.services.anomaly import detect_ip_conflicts
    ln = await _ln(db_session, "docker-host")
    await _subnet(db_session, "172.31.252.0/24", anomaly_enabled=True)
    _arp(db_session, "172.31.252.10", "5a:16:cd:17:0c:01", ln["docker-host"], first_minutes=55, minutes=35)
    _arp(db_session, "172.31.252.10", "72:78:74:e0:7e:02", ln["docker-host"], first_minutes=30, minutes=2)
    await db_session.flush()
    assert not [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "172.31.252.10"]


async def test_a_real_conflict_seen_by_one_router_still_counts(db_session) -> None:
    """同一台路由器看到兩個 MAC（回報者重疊）＝真的在搶，照樣是衝突。"""
    from app.services.anomaly import detect_ip_conflicts
    ln = await _ln(db_session, "router")
    await _subnet(db_session, "172.31.251.0/24", anomaly_enabled=True)
    _arp(db_session, "172.31.251.9", "bc:24:11:10:20:31", ln["router"])
    _arp(db_session, "172.31.251.9", "bc:24:11:a0:b0:c1", ln["router"])
    await db_session.flush()
    assert [r for r in await detect_ip_conflicts(db_session) if r["ip"] == "172.31.251.9"]


async def test_conflicts_respect_the_detection_scope(db_session) -> None:
    """正式環境 2026-10-20：偵測範圍只勾了三個子網路，IP 衝突卻列出 172.17.0.x（每台主機自己的 Docker 網段）。
    失聯 IP、未授權 IP 都照範圍，只有 IP 衝突與 ARP flux 沒有。規則同未授權 IP：IPAM 沒登記的位址
    看它落在哪個有納入的子網路網段裡。"""
    from app.services.anomaly import detect_ip_conflicts
    ln = await _ln(db_session, "r1", "r2")
    on = await _subnet(db_session, "198.51.100.0/24", anomaly_enabled=True)
    off = await _subnet(db_session, "203.0.113.0/24", anomaly_enabled=False)
    await _ip(db_session, off, "203.0.113.40")
    pairs = {"198.51.100.40": ("bc:24:11:10:20:41", "bc:24:11:a0:b0:c2"),     # 範圍內、沒登記
             "203.0.113.40": ("bc:24:11:10:20:42", "bc:24:11:a0:b0:c3"),      # 關掉的子網路
             "172.31.253.40": ("bc:24:11:10:20:43", "bc:24:11:a0:b0:c4")}     # 不在任何子網路
    for ip, (m1, m2) in pairs.items():
        for d in ln.values():
            _arp(db_session, ip, m1, d)
        _arp(db_session, ip, m2, ln["r1"])
        _arp(db_session, ip, m2, ln["r2"])
    await db_session.flush()
    got = {r["ip"] for r in await detect_ip_conflicts(db_session)}
    assert "198.51.100.40" in got
    assert "203.0.113.40" not in got
    assert "172.31.253.40" not in got
    assert on is not None
