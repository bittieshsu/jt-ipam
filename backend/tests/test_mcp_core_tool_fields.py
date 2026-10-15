"""核心盤點類 AI 工具要回傳畫面上看得到的欄位（2026-10-15 盤點，接續 test_mcp_tool_fields.py）。

起因同 list_circuits：工具少給一個欄位，模型就會很有把握地說「系統沒有這個欄位」。盤點發現 IP／子網路／
裝置／地點／機櫃／單位這一批幾乎每支都缺：外鍵只回 UUID、清單沒有總數也不說有沒有被截斷、
四支清單先 LIMIT 再過濾可見性（受限帳號的清單會莫名變短）。這裡逐支釘住。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.mcp.tools import TOOLS

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC).replace(microsecond=0)
IP = "198.51.100.10"
MAC = "00:00:5e:00:53:10"


def _fn(name: str) -> Any:
    return TOOLS[name]["fn"]


async def _user(db, *, display: str = "Alice Lin") -> Any:
    from app.core.security import hash_password
    from app.models.user import User
    u = User(username=f"u-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local",
             display_name=display, password_hash=hash_password("TestPassword2026!"),
             auth_provider="local", is_active=True, is_admin=False)
    db.add(u)
    await db.flush()
    return u


async def _grant(db, user, object_type: str, object_id) -> None:
    from app.models.permission import Permission
    db.add(Permission(object_type=object_type, object_id=object_id, principal_type="user",
                      principal_id=user.id, level="read"))
    await db.flush()


async def _site(db) -> dict[str, Any]:
    """一個每個欄位都填好的子網路、IP、裝置、機櫃、地點、單位。"""
    from app.models.address import IPAddress
    from app.models.customer import Customer
    from app.models.device import Device
    from app.models.ip_range import IPRange
    from app.models.jump_host import JumpHost
    from app.models.location import Location, Rack
    from app.models.oui import OUIVendor
    from app.models.scan_agent import ScanAgent
    from app.models.section import Section
    from app.models.subnet import Subnet
    from app.models.vlan import VLAN, VLANDomain
    from app.models.vrf import VRF

    await db.merge(OUIVendor(prefix="00005E", name="ICANN, IANA Department", short_name="IANA"))
    cust = Customer(name=f"fin-{uuid.uuid4().hex[:6]}", title="Finance", description="Finance dept",
                    contact="Amy", email="fin@example.com", phone="02-1234-5678", address="1 Road, Taipei")
    dom = VLANDomain(name=f"campus-{uuid.uuid4().hex[:6]}")
    vrf = VRF(name=f"blue-{uuid.uuid4().hex[:6]}", rd="65000:1", allow_overlap=False)
    jh = JumpHost(name=f"jump-{uuid.uuid4().hex[:6]}", host="192.0.2.5", username="ops")
    agent = ScanAgent(name=f"agent-{uuid.uuid4().hex[:6]}", description="branch probe", auto_create_ips=True,
                      relay_allowed=True, relay_ports="22,3389", enabled_probes=["icmp", "arp"],
                      enroll_key_hash="f" * 64, api_token_enc=b"secret", api_token_nonce=b"nonce")
    root = Section(name=f"root-{uuid.uuid4().hex[:6]}")
    db.add_all([cust, dom, vrf, jh, agent, root])
    await db.flush()
    loc = Location(name=f"dc-{uuid.uuid4().hex[:6]}", address="2 Road", latitude=25.0330000,
                   longitude=121.5650000, customer_id=cust.id, floor_plan_path="plans/dc.png",
                   description="main room")
    sec = Section(name=f"office-{uuid.uuid4().hex[:6]}", parent_id=root.id, display_order=3,
                  strict_mode=True, customer_id=cust.id, description="office nets")
    db.add_all([loc, sec])
    await db.flush()
    vlan = VLAN(domain_id=dom.id, number=20, name="users", customer_id=cust.id, section_id=sec.id)
    master = Subnet(section_id=sec.id, cidr="198.51.0.0/16")
    db.add_all([vlan, master])
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24", master_subnet_id=master.id, vlan_id=vlan.id,
                 vrf_id=vrf.id, customer_id=cust.id, location_id=loc.id, gateway="198.51.100.1",
                 dns_servers="192.0.2.53", is_pool=True, is_full=True, threshold_pct=80,
                 scan_enabled=True, scan_method=["icmp"], scan_agent_id=agent.id, scan_scope="auto",
                 anomaly_enabled=True, ai_audit_enabled=False, jump_host_id=jh.id, description="office LAN")
    rack = Rack(name=f"R-{uuid.uuid4().hex[:6]}", location_id=loc.id, u_height=42, seq=7, width_mm=600,
                depth_mm=1000, finish="black", numbering="bottom-up", face="rear", expose_svg=True)
    db.add_all([sub, rack])
    await db.flush()
    dev = Device(name=f"srv-{uuid.uuid4().hex[:6]}", type="server", vendor="Dell", model="R650",
                 serial="SN-123", description="web server", location_id=loc.id, rack_id=rack.id,
                 u_position=10, u_size=2, customer_id=cust.id)
    db.add(dev)
    await db.flush()
    ip = IPAddress(subnet_id=sub.id, ip=IP, hostname="web-01", mac=MAC, state="used", owner="ops",
                   description="web", note="do not reboot", device_id=dev.id, switch_port="sw1:ge-0/0/1",
                   switch_port_confident=True, exclude_from_ping=True, liveness_from_device=True,
                   device_evidence_ip="198.51.100.11", ptr_ignore=True, dhcp_reserved=True,
                   in_dhcp_lease=True, is_dhcp_server=True, device_kind="server",
                   device_model="PowerEdge R650", os_guess="Linux 5.15", os_family="linux",
                   discovery_source="scanner", last_seen_scanner=NOW, last_seen_arp=NOW,
                   last_seen_wazuh=NOW, last_seen_zabbix=NOW,
                   arp_seen={"opnsense": NOW.isoformat()}, ssh_enabled=True, rdp_enabled=True,
                   customer_id=cust.id, jump_host_id=jh.id, effective_status="online")
    gw = IPAddress(subnet_id=sub.id, ip="198.51.100.1", hostname="gw", state="used")
    db.add_all([ip, gw, IPRange(subnet_id=sub.id, start_ip="198.51.100.100", end_ip="198.51.100.150",
                                purpose="dhcp", name="pool-a", description="guests")])
    await db.flush()
    dev.primary_ip_id = ip.id
    await db.commit()
    return {"cust": cust, "loc": loc, "sec": sec, "root": root, "vlan": vlan, "dom": dom, "vrf": vrf,
            "jh": jh, "agent": agent, "master": master, "sub": sub, "rack": rack, "dev": dev, "ip": ip}


# ─────────────────────────── IP ───────────────────────────

async def test_search_ip_returns_what_the_ip_list_shows(db_session, admin_user) -> None:
    s = await _site(db_session)
    out = await _fn("search_ip")(db_session, user=admin_user, ip=IP)
    m = out["matches"][0]
    assert m["subnet"] == "198.51.100.0/24"
    assert m["note"] == "do not reboot"
    assert m["switch_port"] == "sw1:ge-0/0/1"
    assert m["device"] == s["dev"].name
    assert m["customer"] == s["cust"].name
    assert m["device_kind"] == "server"
    assert m["os_family"] == "linux"
    assert m["discovery_source"] == "scanner"
    assert m["last_seen_scanner"] is not None and m["last_seen_arp"] is not None
    for flag in ("dhcp_reserved", "is_dhcp_server", "ptr_ignore", "exclude_from_ping", "liveness_from_device"):
        assert m[flag] is True, flag
    assert m["mac_vendor"] == "IANA"


async def test_get_ip_detail_is_complete(db_session, admin_user) -> None:
    s = await _site(db_session)
    d = await _fn("get_ip_detail")(db_session, user=admin_user, ip=IP)
    assert d["found"] is True
    assert (d["device_kind"], d["device_model"]) == ("server", "PowerEdge R650")
    for flag in ("exclude_from_ping", "ptr_ignore", "liveness_from_device", "dhcp_reserved",
                 "in_dhcp_lease", "is_dhcp_server", "switch_port_confident", "ssh_enabled", "rdp_enabled"):
        assert d[flag] is True, flag
    assert d["vnc_enabled"] is False and "sftp_enabled" in d and "bmc_enabled" in d
    assert d["device_evidence_ip"] == "198.51.100.11"
    for k in ("last_seen_arp", "last_seen_wazuh", "last_seen_zabbix"):
        assert d[k] is not None, k
    assert set(d["arp_seen"]) == {"opnsense"}
    assert d["jump_host"] == s["jh"].name and d["console_agent"] is None
    assert d["created_at"] and d["updated_at"]
    assert d["mac_vendor"] == "IANA"
    assert d["is_gateway"] is False
    gw = await _fn("get_ip_detail")(db_session, user=admin_user, ip="198.51.100.1")
    assert gw["is_gateway"] is True
    assert "ptr_ignore" in TOOLS["get_ip_detail"]["description"]


async def test_ip_device_name_hidden_when_device_not_visible(db_session) -> None:
    """IP 看得到、掛的裝置看不到 → 不給裝置名稱（與 REST 清單同一規則）。"""
    s = await _site(db_session)
    u = await _user(db_session)
    await _grant(db_session, u, "subnet", s["sub"].id)
    await db_session.commit()
    d = await _fn("get_ip_detail")(db_session, user=u, ip=IP)
    assert d["found"] is True and d["device"] is None
    m = (await _fn("search_ip")(db_session, user=u, ip=IP))["matches"][0]
    assert m["device"] is None


async def test_list_subnet_ips_rows_have_list_columns_and_total(db_session, admin_user) -> None:
    s = await _site(db_session)
    out = await _fn("list_subnet_ips")(db_session, user=admin_user, subnet_id=str(s["sub"].id), limit=1)
    assert out["total"] == 2 and out["truncated"] is True and out["has_more"] is True
    out = await _fn("list_subnet_ips")(db_session, user=admin_user, subnet_id=str(s["sub"].id))
    row = next(r for r in out["ips"] if r["ip"] == IP)
    assert row["device"] == s["dev"].name
    assert (row["description"], row["note"], row["switch_port"]) == ("web", "do not reboot", "sw1:ge-0/0/1")
    assert row["device_kind"] == "server" and row["os_guess"] == "Linux 5.15"
    assert row["last_seen"] is not None
    assert row["mac_vendor"] == "IANA"
    assert out["truncated"] is False


async def test_ip_history_and_changes_name_the_actor(db_session, admin_user) -> None:
    from app.models.ip_change_log import IPChangeLog
    s = await _site(db_session)
    actor = await _user(db_session, display="Bob Chen")
    db_session.add(IPChangeLog(ip_id=s["ip"].id, subnet_id=s["sub"].id, ip_text=IP, event_type="edit",
                               field="hostname", old_value="old", new_value="web-01", source="manual",
                               actor_user_id=actor.id, note="renamed per ticket 42"))
    await db_session.commit()
    ch = await _fn("recent_ip_changes")(db_session, user=admin_user, ip=IP)
    row = ch["changes"][0]
    assert row["actor"] == "Bob Chen"
    assert row["note"] == "renamed per ticket 42"
    assert row["subnet"] == "198.51.100.0/24"
    assert ch["total"] == 1 and ch["truncated"] is False
    hist = await _fn("get_ip_history")(db_session, user=admin_user, ip=IP)
    ev = next(e for e in hist["events"] if e["kind"] == "change")
    assert ev["actor"] == "Bob Chen" and ev["note"] == "renamed per ticket 42"
    assert hist["subnet"] == "198.51.100.0/24"
    assert hist["truncated"] == {} or not any(hist["truncated"].values())


async def test_recent_ip_changes_says_when_capped(db_session, admin_user) -> None:
    from app.models.ip_change_log import IPChangeLog
    s = await _site(db_session)
    for i in range(3):
        db_session.add(IPChangeLog(ip_id=s["ip"].id, subnet_id=s["sub"].id, ip_text=IP, event_type="edit",
                                   field="note", old_value=str(i), new_value=str(i + 1), source="manual"))
    await db_session.commit()
    ch = await _fn("recent_ip_changes")(db_session, user=admin_user, limit=2)
    assert ch["returned"] == 2 and ch["total"] == 3 and ch["truncated"] is True


async def test_investigate_address_section_has_note_device_and_flags(db_session, admin_user) -> None:
    s = await _site(db_session)
    d = await _fn("investigate_ip")(db_session, admin_user, ip=IP)
    a = d["address"]
    assert a["device"] == s["dev"].name
    assert a["note"] == "do not reboot"
    assert a["customer"] == s["cust"].name
    for flag in ("exclude_from_ping", "liveness_from_device", "ptr_ignore"):
        assert a[flag] is True, flag


# ─────────────────────────── MAC ───────────────────────────

async def test_trace_mac_lists_every_sighting_and_marks_the_access_port(db_session, admin_user) -> None:
    from app.models.device import Device
    from app.models.librenms import ARPEntry, FDBEntry, LibreNMSDevice, LibreNMSInstance
    await _site(db_session)
    core, edge = Device(name="sw-core", type="switch"), Device(name="sw-edge", type="switch")
    inst = LibreNMSInstance(name=f"ln-{uuid.uuid4().hex[:6]}", api_url="https://librenms.example",
                            api_token_enc=b"x", api_token_nonce=b"y")
    db_session.add_all([core, edge, inst])
    await db_session.flush()
    ln_core = LibreNMSDevice(instance_id=inst.id, legacy_device_id=1, hostname="sw-core", sysname="sw-core",
                             jt_ipam_device_id=core.id)
    ln_edge = LibreNMSDevice(instance_id=inst.id, legacy_device_id=2, hostname="sw-edge", sysname="sw-edge",
                             jt_ipam_device_id=edge.id)
    db_session.add_all([ln_core, ln_edge])
    await db_session.flush()
    first = NOW - timedelta(days=3)
    # 核心交換器的上行埠：背後一堆 MAC，而且是最近才看到的那一筆（以前只取最新一筆就答錯）
    db_session.add(FDBEntry(mac=MAC, device_id=ln_core.id, instance_id=inst.id, port_name="xe-0/0/48",
                            vlan_id_num=20, first_seen_at=first, last_seen_at=NOW))
    for i in range(10):
        db_session.add(FDBEntry(mac=f"00:00:5e:00:54:{i:02x}", device_id=ln_core.id, instance_id=inst.id,
                                port_name="xe-0/0/48", vlan_id_num=20))
    db_session.add(FDBEntry(mac=MAC, device_id=ln_edge.id, instance_id=inst.id, port_name="ge-0/0/7",
                            vlan_id_num=20, first_seen_at=first, last_seen_at=NOW - timedelta(minutes=5)))
    db_session.add_all([
        ARPEntry(ip=IP, mac=MAC, source="opnsense", interface="lan", last_seen_at=NOW),
        ARPEntry(ip="198.51.100.99", mac=MAC, source="librenms", last_seen_at=NOW - timedelta(hours=1)),
    ])
    await db_session.commit()

    out = await _fn("trace_mac")(db_session, user=admin_user, mac=MAC)
    roles = {(f["switch"], f["port"]): f["role"] for f in out["fdb_sightings"]}
    assert roles == {("sw-core", "xe-0/0/48"): "uplink", ("sw-edge", "ge-0/0/7"): "access"}
    assert (out["fdb"]["switch"], out["fdb"]["port_name"]) == ("sw-edge", "ge-0/0/7")
    edge_row = next(f for f in out["fdb_sightings"] if f["switch"] == "sw-edge")
    assert edge_row["source"] == "librenms" and edge_row["first_seen"] is not None
    assert edge_row["macs_on_port"] == 1
    assert {a["ip"] for a in out["arp_sightings"]} == {IP, "198.51.100.99"}
    assert out["arp"]["ip"] == IP


# ─────────────────────────── 子網路 ───────────────────────────

async def test_list_subnets_names_and_settings(db_session, admin_user) -> None:
    s = await _site(db_session)
    out = await _fn("list_subnets")(db_session, user=admin_user)
    row = next(r for r in out["subnets"] if r["cidr"] == "198.51.100.0/24")
    assert row["section"] == s["sec"].name
    assert row["master_subnet"] == "198.51.0.0/16"
    assert row["vlan"] == {"number": 20, "name": "users"}
    assert row["vrf"] == s["vrf"].name
    assert row["customer"] == s["cust"].name
    assert row["location"] == s["loc"].name
    assert row["dns_servers"] == "192.0.2.53"
    assert (row["is_pool"], row["is_full"], row["threshold_pct"]) == (True, True, 80)
    assert row["scan_agent"] == s["agent"].name
    assert row["anomaly_enabled"] is True and row["ai_audit_enabled"] is False
    assert row["used"] == 2
    assert out["total"] == 2 and out["truncated"] is False


async def test_list_subnets_skips_archived_like_the_ui(db_session, admin_user) -> None:
    s = await _site(db_session)
    s["master"].archived_at = NOW
    await db_session.commit()
    live = await _fn("list_subnets")(db_session, user=admin_user)
    assert {r["cidr"] for r in live["subnets"]} == {"198.51.100.0/24"}
    arch = await _fn("list_subnets")(db_session, user=admin_user, archived=True)
    assert {r["cidr"] for r in arch["subnets"]} == {"198.51.0.0/16"}
    assert arch["subnets"][0]["archived"] is True
    assert "archived" in TOOLS["list_subnets"]["parameters"]["properties"]


async def test_list_subnets_visibility_is_applied_before_limit(db_session) -> None:
    """受限帳號：看得到的子網路排在後面時，以前 LIMIT 先截掉前面看不到的那幾筆、剩下的又被濾掉 → 空清單。"""
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    hidden = [Subnet(section_id=sec.id, cidr=f"10.{i}.0.0/24") for i in range(3)]
    mine = Subnet(section_id=sec.id, cidr="203.0.113.0/24")
    db_session.add_all([*hidden, mine])
    await db_session.flush()
    u = await _user(db_session)
    await _grant(db_session, u, "subnet", mine.id)
    await db_session.commit()
    out = await _fn("list_subnets")(db_session, user=u, limit=1)
    assert [r["cidr"] for r in out["subnets"]] == ["203.0.113.0/24"]
    assert out["total"] == 1, "總數不可以把看不到的算進去"


async def test_subnet_usage_and_detail_are_complete(db_session, admin_user) -> None:
    s = await _site(db_session)
    u = await _fn("get_subnet_usage")(db_session, user=admin_user, subnet_id=str(s["sub"].id))
    assert u["threshold_pct"] == 80 and u["is_full"] is True
    d = await _fn("get_subnet_detail")(db_session, user=admin_user, subnet_id=str(s["sub"].id))
    assert d["threshold_pct"] == 80
    assert d["location"] == s["loc"].name
    assert d["scan_scope"] == "auto"
    assert d["anomaly_enabled"] is True and d["ai_audit_enabled"] is False
    assert d["jump_host"] == s["jh"].name and d["console_agent"] is None
    assert d["created_at"] and d["updated_at"]
    assert d["vlan"]["domain"] == s["dom"].name
    rng = d["ranges"][0]
    assert (rng["start_ip"], rng["end_ip"], rng["purpose"], rng["name"], rng["description"]) == (
        "198.51.100.100", "198.51.100.150", "dhcp", "pool-a", "guests")
    assert "ranges" in TOOLS["get_subnet_detail"]["description"]


# ─────────────────────────── 區段／VLAN／VRF ───────────────────────────

async def test_list_sections_vlans_vrfs_resolve_names(db_session, admin_user) -> None:
    s = await _site(db_session)
    secs = await _fn("list_sections")(db_session, user=admin_user)
    row = next(r for r in secs["sections"] if r["name"] == s["sec"].name)
    assert row["parent"] == s["root"].name and row["customer"] == s["cust"].name
    assert (row["display_order"], row["strict_mode"], row["subnet_count"]) == (3, True, 2)
    assert secs["total"] == 2 and secs["truncated"] is False

    vl = await _fn("list_vlans")(db_session, user=admin_user)
    v = vl["vlans"][0]
    assert (v["domain"], v["customer"], v["section"]) == (s["dom"].name, s["cust"].name, s["sec"].name)
    assert v["subnet_count"] == 1 and vl["total"] == 1

    vr = await _fn("list_vrfs")(db_session, user=admin_user)
    assert vr["vrfs"][0]["allow_overlap"] is False and vr["vrfs"][0]["subnet_count"] == 1
    assert vr["total"] == 1 and vr["truncated"] is False


async def test_list_sections_visibility_is_applied_before_limit(db_session) -> None:
    from app.models.section import Section
    secs = [Section(name=f"a-{i}") for i in range(3)]
    mine = Section(name="zz-mine")
    db_session.add_all([*secs, mine])
    await db_session.flush()
    u = await _user(db_session)
    await _grant(db_session, u, "section", mine.id)
    await db_session.commit()
    out = await _fn("list_sections")(db_session, user=u, limit=1)
    assert [r["name"] for r in out["sections"]] == ["zz-mine"] and out["total"] == 1


# ─────────────────────────── 地點／機櫃／裝置 ───────────────────────────

async def test_list_locations_and_racks_complete(db_session, admin_user) -> None:
    s = await _site(db_session)
    locs = await _fn("list_locations")(db_session, user=admin_user)
    loc = locs["locations"][0]
    assert (loc["latitude"], loc["longitude"]) == (25.033, 121.565)
    assert loc["customer"] == s["cust"].name and loc["has_floor_plan"] is True
    assert (loc["rack_count"], loc["device_count"]) == (1, 1)
    assert locs["total"] == 1 and locs["truncated"] is False

    racks = await _fn("list_racks")(db_session, user=admin_user)
    r = racks["racks"][0]
    assert (r["seq"], r["width_mm"], r["depth_mm"], r["finish"]) == (7, 600, 1000, "black")
    assert (r["numbering"], r["face"], r["expose_svg"]) == ("bottom-up", "rear", True)
    for k in ("board_mm", "floor_mm", "row_height_mm"):
        assert k in r
    assert racks["total"] == 1 and racks["truncated"] is False


async def test_list_locations_visibility_is_applied_before_limit(db_session) -> None:
    from app.models.location import Location
    others = [Location(name=f"a-{i}") for i in range(3)]
    mine = Location(name="zz-mine")
    db_session.add_all([*others, mine])
    await db_session.flush()
    u = await _user(db_session)
    await _grant(db_session, u, "location", mine.id)
    await db_session.commit()
    out = await _fn("list_locations")(db_session, user=u, limit=1)
    assert [r["name"] for r in out["locations"]] == ["zz-mine"] and out["total"] == 1


async def test_list_devices_has_list_columns(db_session, admin_user) -> None:
    s = await _site(db_session)
    out = await _fn("list_devices")(db_session, user=admin_user)
    d = out["devices"][0]
    assert d["primary_ip"] == IP
    assert d["location"] == s["loc"].name and d["rack"] == s["rack"].name
    assert d["serial"] == "SN-123" and d["description"] == "web server"
    assert d["is_virtual"] is False
    assert d["ip_count"] == 1
    assert out["total"] == 1 and out["truncated"] is False


async def test_get_device_complete_and_ip_rows_detailed(db_session, admin_user) -> None:
    s = await _site(db_session)
    d = await _fn("get_device")(db_session, user=admin_user, device_id=str(s["dev"].id))
    assert d["primary_ip"] == IP
    assert d["is_virtual"] is False
    assert d["created_at"] and d["updated_at"]
    assert d["rack"]["name"] == s["rack"].name
    row = d["ips"][0]
    assert (row["state"], row["switch_port"], row["description"]) == ("used", "sw1:ge-0/0/1", "web")
    assert row["last_seen"] is not None and row["note"] == "do not reboot"


async def test_get_device_by_ambiguous_name_lists_candidates(db_session, admin_user) -> None:
    from app.models.device import Device
    db_session.add_all([Device(name="nas", type="storage"), Device(name="NAS", type="server")])
    await db_session.commit()
    out = await _fn("get_device")(db_session, user=admin_user, name="nas")
    assert out["ambiguous"] is True
    assert len(out["candidates"]) == 2 and all(c["id"] for c in out["candidates"])


async def test_get_device_by_name_ignores_devices_the_user_cannot_see(db_session) -> None:
    from app.models.device import Device
    a, b = Device(name="nas", type="storage"), Device(name="nas", type="server")
    db_session.add_all([a, b])
    await db_session.flush()
    u = await _user(db_session)
    await _grant(db_session, u, "device", b.id)
    await db_session.commit()
    out = await _fn("get_device")(db_session, user=u, name="nas")
    assert out["id"] == str(b.id), "看不到的同名裝置不可以出現，也不可以造成假的「不明確」"


# ─────────────────────────── 單位 ───────────────────────────

async def test_customers_list_and_summary_complete(db_session, admin_user) -> None:
    s = await _site(db_session)
    out = await _fn("list_customers")(db_session, user=admin_user)
    c = out["customers"][0]
    assert c["description"] == "Finance dept" and c["subnet_count"] == 1
    assert out["total"] == 1 and out["truncated"] is False
    summ = await _fn("get_customer_summary")(db_session, user=admin_user, customer_id=str(s["cust"].id))
    assert summ["description"] == "Finance dept"
    assert (summ["email"], summ["phone"], summ["address"]) == ("fin@example.com", "02-1234-5678", "1 Road, Taipei")


async def test_list_customers_visibility_is_applied_before_limit(db_session) -> None:
    from app.models.customer import Customer
    others = [Customer(name=f"a-{i}") for i in range(3)]
    mine = Customer(name="zz-mine")
    db_session.add_all([*others, mine])
    await db_session.flush()
    u = await _user(db_session)
    await _grant(db_session, u, "customer", mine.id)
    await db_session.commit()
    out = await _fn("list_customers")(db_session, user=u, limit=1)
    assert [r["name"] for r in out["customers"]] == ["zz-mine"] and out["total"] == 1


# ─────────────────────────── 統計／搜尋／代理／申請／Wazuh ───────────────────────────

async def test_stats_overview_counts_vrfs_and_vpns_and_says_so(db_session, admin_user) -> None:
    await _site(db_session)
    out = await _fn("stats_overview")(db_session, user=admin_user)
    assert out["vrfs"] == 1 and "vpn_tunnels" in out
    desc = TOOLS["stats_overview"]["description"]
    for word in ("VRF", "VPN", "VM", "circuit", "cable"):
        assert word.lower() in desc.lower(), word


def test_global_search_description_lists_every_type() -> None:
    desc = TOOLS["global_search"]["description"].lower()
    for word in ("section", "customer", "rack", "location", "vm", "nat", "dns", "firewall", "request", "vpn"):
        assert word in desc, word


async def test_list_scan_agents_settings_without_secrets(db_session, admin_user) -> None:
    s = await _site(db_session)
    out = await _fn("list_scan_agents")(db_session, user=admin_user)
    a = out["agents"][0]
    assert a["description"] == "branch probe" and a["auto_create_ips"] is True
    assert a["relay_allowed"] is True and a["relay_ports"] == "22,3389"
    assert a["subnet_count"] == 1 and a["has_key"] is True
    assert "probe_intervals" in a and "load" in a and "tools" in a
    flat = repr(out)
    assert "f" * 64 not in flat and "secret" not in flat, "代理金鑰與權杖不可以出現"
    assert out["total"] == 1 and out["truncated"] is False
    assert s["agent"].name == a["name"]


async def test_list_ip_requests_shows_cidr_ip_and_reason(db_session, admin_user) -> None:
    from app.models.ip_request import IPRequest
    s = await _site(db_session)
    req_user = await _user(db_session, display="Carol Wu")
    db_session.add_all([
        IPRequest(requester_user_id=req_user.id, subnet_id=s["sub"].id, purpose="new printer",
                  status="rejected", rejected_reason="use the printer VLAN"),
        IPRequest(requester_user_id=req_user.id, subnet_id=s["sub"].id, purpose="web",
                  status="fulfilled", allocated_ip_id=s["ip"].id),
    ])
    await db_session.commit()
    out = await _fn("list_ip_requests")(db_session, user=admin_user)
    by = {r["purpose"]: r for r in out["requests"]}
    assert by["new printer"]["rejected_reason"] == "use the printer VLAN"
    assert by["new printer"]["subnet_cidr"] == "198.51.100.0/24"
    assert by["web"]["allocated_ip"] == IP
    assert by["web"]["requester"] == "Carol Wu"
    assert out["total"] == 2 and out["truncated"] is False
    assert "approver" in TOOLS["list_ip_requests"]["description"].lower()


async def test_wazuh_missing_agents_rows_have_context(db_session, admin_user) -> None:
    s = await _site(db_session)
    out = await _fn("wazuh_missing_agents")(db_session, user=admin_user, subnet_cidr="198.51.100.0/24")
    row = next(r for r in out["missing"] if r["ip"] == IP)
    assert row["subnet"] == "198.51.100.0/24"
    assert row["section"] == s["sec"].name and row["customer"] == s["cust"].name
    assert row["device_kind"] == "server" and row["effective_status"] == "online"
    assert out["truncated"] is False and out["total"] == out["missing_count"]


# ─────────────────────────── 共通：清單一定說得出總數與是否截斷 ───────────────────────────

_LIST_TOOLS = {
    "list_subnets": ("subnets", {}), "list_vlans": ("vlans", {}), "list_vrfs": ("vrfs", {}),
    "list_sections": ("sections", {}), "list_locations": ("locations", {}), "list_racks": ("racks", {}),
    "list_devices": ("devices", {}), "list_customers": ("customers", {}),
    "list_scan_agents": ("agents", {}), "list_ip_requests": ("requests", {}),
    "recent_ip_changes": ("changes", {}),
}


async def test_every_list_tool_reports_total_and_truncated(db_session, admin_user) -> None:
    await _site(db_session)
    for name, (key, args) in _LIST_TOOLS.items():
        out = await _fn(name)(db_session, user=admin_user, **args)
        assert "total" in out and "truncated" in out, name
        assert isinstance(out["truncated"], bool), name
        assert out["truncated"] == (out["total"] > len(out[key])), name
