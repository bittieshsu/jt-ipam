"""大子網路改成兩層掃描（客戶 2026-10-11：前人把主站台設成 10.0.0.0/8，掃描直接炸掉，只好把子網路改成 /23）。

以前掃描代理只拿到 CIDR，每輪照順序掃下一段 4,096 個位址：/8 要 4,096 輪（約 14 天）才繞一圈，
上線門檻 30 分鐘 → 主站台的設備幾乎永遠被判成離線，系統的建議也只有「把子網路切小」，
結果 IPAM 只能把網段記錯（設備實際的遮罩是 /8）。

改法：
- 已知位址（已登記的 IP、7 天內 ARP 看過的）每一輪都掃 → 上線狀態不管子網路多大都準確
- 其餘位址每輪抽一段慢慢找新設備（探索），不算進上線偵測的負載
- 每個子網路可以選掃描範圍：自動（預設，4,096 個以內照舊全掃）／全部／只掃已知位址／只掃指定的位址範圍
"""
from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.address import IPAddress
from app.models.ip_range import IPRange
from app.models.librenms import ARPEntry
from app.models.scan_agent import ScanAgent
from app.models.section import Section
from app.models.subnet import Subnet
from app.services import scan_load, scan_plan


def _ints(runs: list[list[str]]) -> list[int]:
    out: list[int] = []
    for a, b in runs:
        out += list(range(int(ipaddress.ip_address(a)), int(ipaddress.ip_address(b)) + 1))
    return out


# ─────────────────── 伺服器：掃描計畫 ───────────────────

def test_runs_compress_consecutive_addresses() -> None:
    ips = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.7", "10.0.0.8", "10.0.1.5"]
    assert scan_plan.to_runs(int(ipaddress.ip_address(x)) for x in ips) == [
        ["10.0.0.1", "10.0.0.3"], ["10.0.0.7", "10.0.0.8"], ["10.0.1.5", "10.0.1.5"]]


def test_ranges_are_merged_without_expanding_them() -> None:
    """位址範圍可以很大（一段 /16），合併時不可以把每個位址展開。"""
    got = scan_plan.merge_ranges([("10.1.0.0", "10.1.255.255"), ("10.1.128.0", "10.2.0.10"), ("10.9.0.1", "10.9.0.1")])
    assert got == [["10.1.0.0", "10.2.0.10"], ["10.9.0.1", "10.9.0.1"]]


async def _subnet(db, cidr: str, scope: str = "auto") -> Subnet:
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    s = Subnet(section_id=sec.id, cidr=cidr, scan_enabled=True, scan_scope=scope)
    db.add(s)
    await db.flush()
    return s


async def test_small_subnet_in_auto_mode_is_swept_as_before(db_session) -> None:
    s = await _subnet(db_session, "192.0.2.0/24")
    assert (await scan_plan.plans(db_session, [s]))[s.id] is None


async def test_large_subnet_in_auto_mode_scans_known_addresses_and_discovers_the_rest(db_session) -> None:
    s = await _subnet(db_session, "10.0.0.0/8")
    for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.20.30.40"):
        db_session.add(IPAddress(subnet_id=s.id, ip=ip))
    now = datetime.now(UTC)
    db_session.add(ARPEntry(ip="10.0.0.9", mac="02:00:5e:00:53:09", subnet_id=s.id, last_seen_at=now))
    db_session.add(ARPEntry(ip="10.0.0.99", mac="02:00:5e:00:53:63", subnet_id=s.id,
                            last_seen_at=now - timedelta(days=30)))   # 太久以前：不算已知
    await db_session.flush()
    plan = (await scan_plan.plans(db_session, [s]))[s.id]
    assert plan["mode"] == "targets"
    assert [str(ipaddress.ip_address(x)) for x in _ints(plan["known"])] == \
        ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.9", "10.20.30.40"]
    assert plan["discover"]["per_cycle"] == scan_plan.DISCOVER_PER_CYCLE


async def test_neighbourhood_of_known_devices_is_discovered_first(db_session) -> None:
    """客戶 2026-10-20：10.0.0.0/8 裡新接的印表機 10.0.0.54，五、六個小時都沒被發現 —— 探索每輪 1,024 個，
    /8 要約 57 天才繞完一圈。新設備最可能出現在已經有設備的 /24 裡（.52、.55 都在），這些 /24 其餘的位址
    要放進「鄰近探索」，每輪優先掃、很快繞完一圈。"""
    s = await _subnet(db_session, "10.0.0.0/8")
    for ip in ("10.0.0.52", "10.0.0.55", "10.20.30.40"):
        db_session.add(IPAddress(subnet_id=s.id, ip=ip))
    await db_session.flush()
    plan = (await scan_plan.plans(db_session, [s]))[s.id]
    near = {str(ipaddress.ip_address(x)) for x in _ints(plan["discover"]["near"])}
    assert "10.0.0.54" in near
    assert "10.20.30.41" in near
    assert not {"10.0.0.52", "10.0.0.55", "10.20.30.40"} & near        # 已知的每輪都掃，不重複
    assert "10.0.0.0" not in near                                       # 網路位址不是主機
    assert len(near) == (255 - 2) + (256 - 1)          # /8 裡 .0 與 .255 也是主機位址，只有 10.0.0.0 不是
    assert "10.0.1.1" not in near                                       # 沒有設備的 /24 照舊慢慢探索
    assert plan["discover"]["near_per_cycle"] == scan_plan.NEAR_PER_CYCLE


async def test_neighbourhood_is_capped_to_the_busiest_blocks(db_session, monkeypatch) -> None:
    monkeypatch.setattr(scan_plan, "NEAR_BLOCK_CAP", 2)
    s = await _subnet(db_session, "10.0.0.0/8")
    for ip in ("10.1.0.1", "10.1.0.2", "10.1.0.3", "10.2.0.1", "10.2.0.2", "10.3.0.1"):
        db_session.add(IPAddress(subnet_id=s.id, ip=ip))
    await db_session.flush()
    near = _ints((await scan_plan.plans(db_session, [s]))[s.id]["discover"]["near"])
    blocks = {n >> 8 for n in near}
    assert blocks == {int(ipaddress.ip_address("10.1.0.0")) >> 8, int(ipaddress.ip_address("10.2.0.0")) >> 8}


async def test_scope_all_keeps_the_full_sweep_even_for_a_large_subnet(db_session) -> None:
    s = await _subnet(db_session, "10.0.0.0/16", scope="all")
    assert (await scan_plan.plans(db_session, [s]))[s.id] is None


async def test_scope_known_never_discovers(db_session) -> None:
    s = await _subnet(db_session, "192.0.2.0/24", scope="known")
    db_session.add(IPAddress(subnet_id=s.id, ip="192.0.2.10"))
    await db_session.flush()
    plan = (await scan_plan.plans(db_session, [s]))[s.id]
    assert plan == {"mode": "targets", "known": [["192.0.2.10", "192.0.2.10"]], "discover": None}


async def test_scope_ranges_scans_only_the_subnet_ranges(db_session) -> None:
    s = await _subnet(db_session, "10.0.0.0/8", scope="ranges")
    db_session.add(IPRange(subnet_id=s.id, start_ip="10.0.0.1", end_ip="10.0.1.254", purpose="other"))
    db_session.add(IPRange(subnet_id=s.id, start_ip="10.5.0.1", end_ip="10.5.0.50", purpose="dhcp"))
    db_session.add(IPAddress(subnet_id=s.id, ip="10.9.9.9"))     # 範圍外：這個模式不掃
    await db_session.flush()
    plan = (await scan_plan.plans(db_session, [s]))[s.id]
    assert plan == {"mode": "targets", "known": [["10.0.0.1", "10.0.1.254"], ["10.5.0.1", "10.5.0.50"]],
                    "discover": None}


async def test_ipv6_subnets_keep_the_old_behaviour(db_session) -> None:
    """代理目前只掃 IPv4：IPv6 子網路不送計畫（照舊由代理略過）。"""
    s = await _subnet(db_session, "2001:db8::/64", scope="known")
    assert (await scan_plan.plans(db_session, [s]))[s.id] is None


async def test_poll_hands_the_plan_to_the_agent(client, db_session) -> None:
    from app.api.v1.endpoints.scan_agents import _key_hash
    raw = "p" * 40
    agent = ScanAgent(name=f"a-{uuid.uuid4().hex[:6]}", enroll_key_hash=_key_hash(raw), enabled=True,
                      enabled_probes=["icmp"])
    db_session.add(agent)
    await db_session.flush()
    big = await _subnet(db_session, "10.0.0.0/8")
    small = await _subnet(db_session, "192.0.2.0/24")
    big.scan_agent_id = small.scan_agent_id = agent.id
    db_session.add(IPAddress(subnet_id=big.id, ip="10.0.0.5"))
    await db_session.commit()
    body = (await client.get("/api/v1/scan-agents/poll", headers={"X-Agent-Key": raw})).json()
    by_cidr = {x["cidr"]: x for x in body["subnets"]}
    assert by_cidr["10.0.0.0/8"]["plan"]["known"] == [["10.0.0.5", "10.0.0.5"]]
    assert "plan" not in by_cidr["192.0.2.0/24"], "小子網路照舊：舊代理與新代理行為一樣"


async def test_subnet_api_accepts_the_scan_scope(client, auth_headers, db_session) -> None:
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.commit()
    r = await client.post("/api/v1/subnets", headers=auth_headers,
                          json={"section_id": str(sec.id), "cidr": "10.0.0.0/8", "scan_scope": "known"})
    assert r.status_code == 201, r.text
    assert r.json()["scan_scope"] == "known"
    r = await client.patch(f"/api/v1/subnets/{r.json()['id']}", headers=auth_headers, json={"scan_scope": "bogus"})
    assert r.status_code == 422


# ─────────────────── 代理：照計畫挑這一輪要掃的位址 ───────────────────

def _agent():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / "agent" / "jt_ipam_agent.py"
    spec = importlib.util.spec_from_file_location(f"jt_agent_plan_{uuid.uuid4().hex[:6]}", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_agent_without_a_plan_sweeps_like_before(monkeypatch) -> None:
    mod = _agent()
    monkeypatch.setattr(mod, "MAX_HOSTS", 100)
    hosts, total, info = mod._plan_hosts("s1", "198.18.0.0/24", None)
    assert (len(hosts), total, info["rounds"]) == (100, 254, 3)


def test_agent_scans_known_every_cycle_plus_a_discovery_window(monkeypatch) -> None:
    mod = _agent()
    plan = {"mode": "targets", "known": [["10.0.0.1", "10.0.0.3"]], "discover": {"per_cycle": 4}}
    h1, total, i1 = mod._plan_hosts("big", "10.0.0.0/8", plan)
    h2, _, i2 = mod._plan_hosts("big", "10.0.0.0/8", plan)
    assert total == 16777214
    # 每一輪都有已知的三個；探索視窗往後推，跳過已知的
    assert h1[:3] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"] and h2[:3] == h1[:3]
    assert h1[3:] == ["10.0.0.4"], "第一個視窗 10.0.0.1–4，已知的不重複探測"
    assert h2[3:] == ["10.0.0.5", "10.0.0.6", "10.0.0.7", "10.0.0.8"]
    assert (i1["mode"], i1["known"], i1["rounds"]) == ("targets", 3, 1), "上線偵測一輪就繞完已知位址"
    assert i1["discover_rounds"] == -(-16777214 // 4) and i2["discover_chunk"] == 2


def test_agent_discovers_the_neighbourhood_first(monkeypatch) -> None:
    """鄰近探索：每輪先掃 near 的下一段（繞完從頭），再掃全域探索的視窗。舊代理不認得 near，照舊只有全域探索。"""
    mod = _agent()
    plan = {"mode": "targets", "known": [["10.0.0.52", "10.0.0.52"], ["10.0.0.55", "10.0.0.55"]],
            "discover": {"per_cycle": 2, "near": [["10.0.0.50", "10.0.0.51"], ["10.0.0.53", "10.0.0.54"]],
                         "near_per_cycle": 3}}
    h1, _, i1 = mod._plan_hosts("nb", "10.0.0.0/8", plan)
    h2, _, i2 = mod._plan_hosts("nb", "10.0.0.0/8", plan)
    assert h1[:2] == ["10.0.0.52", "10.0.0.55"]
    assert h1[2:5] == ["10.0.0.50", "10.0.0.51", "10.0.0.53"]
    assert h2[2:3] == ["10.0.0.54"], "第二輪接著掃 near 剩下的（四個位址、每輪三個）"
    assert h1[5:] == ["10.0.0.1", "10.0.0.2"], "全域探索照舊往後推"
    assert (i1["near"], i1["near_rounds"]) == (4, 2)
    assert i2["near_chunk"] == 2


def test_agent_rotates_known_addresses_beyond_the_cycle_limit(monkeypatch) -> None:
    mod = _agent()
    monkeypatch.setattr(mod, "MAX_HOSTS", 100)
    plan = {"mode": "targets", "known": [["10.1.0.0", "10.1.255.255"]], "discover": None}   # 65,536 個
    h1, _, i1 = mod._plan_hosts("r", "10.0.0.0/8", plan)
    h2, _, _ = mod._plan_hosts("r", "10.0.0.0/8", plan)
    assert (len(h1), h1[0], h2[0]) == (100, "10.1.0.0", "10.1.0.100")
    assert (i1["known"], i1["rounds"], i1.get("discover_rounds")) == (65536, 656, None)


# ─────────────────── 負載評估：只拿已知位址判斷超載 ───────────────────

def _cycle(duration: float, interval: int = 300, subnets: list | None = None) -> dict:
    return {"duration_s": duration, "interval_s": interval, "subnets": subnets or [], "heavy_backlog": 0}


def test_discovery_does_not_count_as_a_coverage_gap() -> None:
    """/8 的探索要幾十天才繞完一圈，但已知位址每一輪都掃到 → 不是超載、不會被判成離線。"""
    subs = [{"cidr": "10.0.0.0/8", "hosts": 1029, "total_hosts": 16777214, "alive": 300, "duration_s": 30,
             "truncated": False, "mode": "targets", "known": 5, "chunk": 1, "rounds": 1,
             "discover_chunk": 7, "discover_rounds": 16384}]
    ev = scan_load.evaluate(_cycle(30, subnets=subs), online_minutes=30)
    assert ev["coverage_gap"] == []
    assert not [x for x in ev["suggestions"] if x["code"] in ("rotating_large", "rotating_known", "split_large")]
    info = next(x for x in ev["suggestions"] if x["code"] == "discovery_pass")
    assert info["params"] == {"cidr": "10.0.0.0/8", "total": 16777214, "hours": round(16384 * 300 / 3600)}


@pytest.mark.parametrize("rounds,interval,gap", [(4, 300, False), (10, 300, True)])
def test_known_addresses_that_take_too_long_still_count(rounds, interval, gap) -> None:
    subs = [{"cidr": "10.0.0.0/8", "hosts": 4096, "total_hosts": 16777214, "alive": 900, "duration_s": 40,
             "truncated": False, "mode": "targets", "known": rounds * 4096, "chunk": 1, "rounds": rounds}]
    ev = scan_load.evaluate(_cycle(40, interval=interval, subnets=subs), online_minutes=30)
    assert (ev["coverage_gap"] == ["10.0.0.0/8"]) is gap
    s = next(x for x in ev["suggestions"] if x["code"] == "rotating_known")
    assert s["params"]["known"] == rounds * 4096, "講的是已知位址有幾個，不是整個網段"
