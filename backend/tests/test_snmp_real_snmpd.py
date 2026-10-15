"""SNMP 收集對真的 net-snmp snmpd 跑一遍（選用：沒設 JTIPAM_TEST_SNMPD 就跳過）。

假工具驗得到參數與解析，驗不到「net-snmp 真的吃得下我們寫的 snmp.conf」（引號、#、v3 的協定名稱）、
真實設備回的格式（ARP 新表的索引、介面位址表裡的廣播位址）。開發機上起一個只綁 127.0.0.1 的 snmpd：

    agentAddress udp:127.0.0.1:16161
    rocommunity testcomm 127.0.0.1
    createUser jtuser SHA-256 "authpass123" AES "privpass123"
    rouser jtuser priv

    JTIPAM_TEST_SNMPD=127.0.0.1:16161:testcomm   （v3 另設 JTIPAM_TEST_SNMPD_V3=jtuser:authpass123:privpass123）

流程：代理的程式碼（真的 snmpget／snmpbulkwalk）讀 snmpd → 回報給伺服器的 /scan-agents/snmp-report →
檢查 ARP 補到 MAC、介面位址的上線證據、介面進了裝置連接埠。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("JTIPAM_TEST_SNMPD") or not shutil.which("snmpbulkwalk"),
    reason="JTIPAM_TEST_SNMPD not set (or net-snmp tools missing)")

_AGENT = pathlib.Path(__file__).resolve().parents[2] / "agent" / "jt_ipam_agent.py"


def _agent():
    spec = importlib.util.spec_from_file_location(f"jt_agent_real_{uuid.uuid4().hex[:6]}", _AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _creds() -> list[dict]:
    host, port, community = os.environ["JTIPAM_TEST_SNMPD"].split(":", 2)
    out = [{"credential_id": "v2c", "version": "v2c", "community": community, "timeout_s": 2, "retries": 0}]
    v3 = os.environ.get("JTIPAM_TEST_SNMPD_V3")
    if v3:
        user, auth, priv = v3.split(":", 2)
        out.append({"credential_id": "v3", "version": "v3", "username": user, "security_level": "authPriv",
                    "auth_protocol": "SHA-256", "auth_passphrase": auth, "priv_protocol": "AES-128",
                    "priv_passphrase": priv, "timeout_s": 2, "retries": 0})
    return out


def test_agent_reads_a_real_snmpd() -> None:
    mod = _agent()
    host, port, _c = os.environ["JTIPAM_TEST_SNMPD"].split(":", 2)
    target = mod._snmp_target_ok({"target_id": "t", "ip": host, "port": int(port),
                                  "sections": {"arp": 300, "fdb": 900, "neighbors": 3600, "interfaces": 3600}})
    for raw in _creds():
        out = mod._snmp_collect(target, mod._snmp_cred_ok(raw), list(target["sections"]),
                                mod._snmp_budget({"pause_ms": 0}))
        assert out.get("error") is None, (raw["credential_id"], out.get("error"))
        assert out["system"]["sys_name"] and out["system"]["sys_object_id"]
        secs = out["sections"]
        assert secs["interfaces"]["status"] == "ok" and secs["interfaces"]["rows"]
        assert secs["arp"]["status"] in ("ok", "absent")
        assert secs["fdb"]["status"] in ("ok", "absent")          # Linux 的 snmpd 沒有 BRIDGE-MIB
        assert secs["neighbors"]["status"] in ("ok", "absent")
        # 介面位址只留單播（Linux 會把廣播位址也列進 ipAddressTable）
        assert all(not a["ip"].endswith(".255") for a in secs["interfaces"]["addresses"])
    bad = mod._snmp_collect(target, mod._snmp_cred_ok({**_creds()[0], "community": "wrong-" + uuid.uuid4().hex[:6],
                                                       "timeout_s": 1}), ["arp"], mod._snmp_budget({"pause_ms": 0}))
    assert bad["error"] and bad["error_code"] == "snmp_unreachable"


async def test_real_snmpd_report_lands_in_ipam(client, auth_headers, db_session) -> None:
    from app.api.v1.endpoints.scan_agents import _key_hash
    from app.models.address import IPAddress
    from app.models.device import Device
    from app.models.physical import DevicePort
    from app.models.scan_agent import ScanAgent
    from app.models.section import Section
    from app.models.subnet import Subnet
    from sqlalchemy import select

    mod = _agent()
    host, port, community = os.environ["JTIPAM_TEST_SNMPD"].split(":", 2)
    target = mod._snmp_target_ok({"target_id": "t", "ip": host, "port": int(port),
                                  "sections": {"arp": 300, "interfaces": 3600}})
    payload = mod._snmp_collect(target, mod._snmp_cred_ok(_creds()[0]), ["arp", "interfaces"],
                                mod._snmp_budget({"pause_ms": 0}))
    arp = [r for r in payload["sections"]["arp"]["rows"] if ":" not in r[0]]
    assert arp, "這台 snmpd 的 ARP 表是空的：換一台有鄰居的主機跑"
    neighbour_ip, neighbour_mac = arp[0][0], arp[0][1]

    key = "real-snmpd-" + "k" * 30
    sec = Section(name=f"real-{uuid.uuid4().hex[:6]}")
    agent = ScanAgent(name=f"real-{uuid.uuid4().hex[:4]}", enroll_key_hash=_key_hash(key), enabled=True,
                      agent_version=mod.AGENT_VERSION, is_local=True,
                      tools=[{"name": "snmpbulkwalk", "installed": True, "version": "5.9"}])
    db_session.add_all([sec, agent])
    await db_session.flush()
    import ipaddress
    net = ipaddress.ip_network(f"{neighbour_ip}/24", strict=False)
    sub = Subnet(cidr=str(net), section_id=sec.id)
    dev = Device(name="snmpd-host", type="server")
    db_session.add_all([sub, dev])
    await db_session.flush()
    db_session.add(IPAddress(subnet_id=sub.id, ip=neighbour_ip, state="used"))
    await db_session.commit()
    dev_id = dev.id
    r = await client.post("/api/v1/snmp/credentials", json={"name": "real", "version": "v2c",
                                                            "community": community, "is_default": True},
                          headers=auth_headers)
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/snmp/targets", json={"device_id": str(dev_id), "ip_override": host,
                                                        "port": int(port), "collector": "server",
                                                        "collect_fdb": False, "collect_neighbors": False},
                          headers=auth_headers)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    hdr = {"X-Agent-Key": key, "X-Agent-Version": mod.AGENT_VERSION, "X-Agent-Tools": mod._tools_header()}
    poll = (await client.get("/api/v1/scan-agents/poll", headers=hdr)).json()
    assert [t["target_id"] for t in poll["snmp"]] == [tid]
    # 代理用輪詢拿到的內容（真的憑證）再讀一次、回報
    mod._snmp_set_assigned(poll["snmp"], poll["snmp_budget"])
    spec = mod._SNMP_STATE["targets"][0]
    rep = mod._snmp_collect(spec, spec["credential"], list(spec["sections"]), mod._snmp_budget({"pause_ms": 0}))
    rep["target_id"] = tid
    r = await client.post("/api/v1/scan-agents/snmp-report", json=rep, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "success", r.json()
    db_session.expire_all()
    got = (await db_session.execute(select(IPAddress).where(IPAddress.ip == neighbour_ip))).scalars().one()
    assert str(got.mac) == neighbour_mac and got.mac_source == "snmp"
    assert set(got.arp_seen or {}) & {"arp:snmp", "arp:snmp_live"}
    ports = (await db_session.execute(select(DevicePort).where(DevicePort.device_id == dev_id))).scalars().all()
    assert ports, "介面沒有進裝置連接埠"
