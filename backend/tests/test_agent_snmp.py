"""掃描代理的 SNMP 收集（代理 1.19.0）：用假的 snmpget／snmpbulkwalk 放在 PATH 上驗證代理端的行為。

要守住的事（memory: project_snmp_collector_design 的安全原則）：
- 憑證**不在 argv**（ps 看得到），只寫在 0700 暫存目錄裡的 0600 snmp.conf，用完就刪
- 憑證有控制字元（換行）一律拒收：換行等於在 snmp.conf 裡插入任意設定
- 只讀寫死的 OID 允許清單；伺服器給的工作種類、目標、預算代理自己再驗一次
- 解析：ARP（新表有狀態、舊表沒有）、Q-BRIDGE／BRIDGE-MIB 的 MAC 表（橋接埠 → ifName）、
  Cisco 逐 VLAN（v2c 用 community@vlan、v3 用 context vlan-N）、LLDP 與 CDP 鄰居
- 列數上限：超過就停、標成未完成（伺服器就不會清舊資料）
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import sys
import uuid

import pytest

_AGENT = pathlib.Path(__file__).resolve().parents[2] / "agent" / "jt_ipam_agent.py"


def _agent():
    spec = importlib.util.spec_from_file_location(f"jt_agent_snmp_{uuid.uuid4().hex[:6]}", _AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_FAKE = r'''#!{python}
import json, os, stat, sys
LOG = {log!r}
FIX = json.load(open({fix!r}))
conf_dir = os.environ.get("SNMPCONFPATH", "")
path = os.path.join(conf_dir, "snmp.conf")
conf = open(path).read()
rec = {{"tool": os.path.basename(sys.argv[0]), "argv": sys.argv[1:], "conf": conf,
       "mode": stat.S_IMODE(os.stat(path).st_mode), "dmode": stat.S_IMODE(os.stat(conf_dir).st_mode),
       "dir": conf_dir, "env": sorted(os.environ)}}
with open(LOG, "a") as f:
    f.write(json.dumps(rec) + "\n")
args = sys.argv[1:]
ctx = args[args.index("-n") + 1] if "-n" in args else ""
comm = ""
for line in conf.splitlines():
    if line.startswith("defCommunity "):
        comm = line.split(" ", 1)[1]
suffix = comm.split("@", 1)[1] if "@" in comm else ""
oids = [a for a in args if a[:1].isdigit() and a.count(".") >= 3]
for oid in oids:
    key = (ctx + "|" + oid) if ctx else (("@" + suffix + "|" + oid) if suffix else oid)
    val = FIX.get(key, FIX.get(oid))
    if isinstance(val, dict):
        sys.stderr.write(val.get("stderr", "") + "\n")
        sys.exit(val.get("exit", 1))
    if val is None:
        print("." + oid + " No Such Object available on this agent at this OID")
    else:
        sys.stdout.write(val)
'''


def _h(text: str) -> str:
    return '"' + " ".join(f"{b:02X}" for b in text.encode()) + ' "'


def _hb(*bs: int) -> str:
    return '"' + " ".join(f"{b:02X}" for b in bs) + ' "'


def _lines(col: str, rows: list[tuple[str, str]]) -> str:
    return "".join(f".{col}.{idx} {val}\n" for idx, val in rows)


@pytest.fixture
def fake_snmp(tmp_path, monkeypatch):
    """回傳 (代理模組, 設定答案的函式, 讀取紀錄的函式)。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    fix = tmp_path / "fix.json"
    fix.write_text("{}")
    for tool in ("snmpget", "snmpbulkwalk"):
        p = bindir / tool
        p.write_text(_FAKE.format(python=sys.executable, log=str(log), fix=str(fix)))
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    mod = _agent()

    def answer(mapping: dict) -> None:
        fix.write_text(json.dumps(mapping))

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]

    return mod, answer, calls


_BUDGET = {"pause_ms": 0, "target_seconds": 120, "walk_seconds": 30, "max_rows": 1000}
_V2C = {"credential_id": "c1", "version": "v2c", "community": "s3cret-Comm!", "timeout_s": 2, "retries": 0}
_V3 = {"credential_id": "c2", "version": "v3", "username": "monitor", "security_level": "authPriv",
       "auth_protocol": "SHA-256", "auth_passphrase": "auth-Pass-123", "priv_protocol": "AES-128",
       "priv_passphrase": "priv-Pass-456", "timeout_s": 2, "retries": 0}


def _sys(name: str = "core-sw", oid: str = ".1.3.6.1.4.1.9.1.1208") -> dict:
    return {"1.3.6.1.2.1.1.1.0": f".1.3.6.1.2.1.1.1.0 {_h('Cisco IOS Software, C2960X')}\n",
            "1.3.6.1.2.1.1.2.0": f".1.3.6.1.2.1.1.2.0 {oid}\n",
            "1.3.6.1.2.1.1.3.0": ".1.3.6.1.2.1.1.3.0 123456\n",
            "1.3.6.1.2.1.1.5.0": f".1.3.6.1.2.1.1.5.0 {_h(name)}\n"}


def _collect(mod, cred, sections, extra_budget=None):
    budget = mod._snmp_budget({**_BUDGET, **(extra_budget or {})})
    target = mod._snmp_target_ok({"target_id": "t1", "ip": "192.0.2.1", "port": 161,
                                  "sections": dict.fromkeys(sections, 300)})
    return mod._snmp_collect(target, mod._snmp_cred_ok(cred), sections, budget)


# ─────────────────── 憑證不落在 argv、只在 0600 暫存檔 ───────────────────
def test_v2c_community_never_on_argv_only_in_private_conf(fake_snmp) -> None:
    mod, answer, calls = fake_snmp
    answer(_sys())
    out = _collect(mod, _V2C, [])
    assert out["system"]["sys_name"] == "core-sw"
    assert out["system"]["sys_object_id"] == "1.3.6.1.4.1.9.1.1208"
    recs = calls()
    assert recs, "假工具沒被呼叫"
    for r in recs:
        assert not any("s3cret" in a for a in r["argv"]), "社群字串出現在命令列（ps 看得到）"
        assert "defCommunity s3cret-Comm!" in r["conf"]
        assert "defVersion 2c" in r["conf"]
        assert r["mode"] == 0o600 and r["dmode"] == 0o700
        assert not os.path.exists(r["dir"]), "暫存的 snmp.conf 沒有刪掉"
        # 環境變數只留必要的（不把代理的金鑰帶進子行程）
        assert "JT_IPAM_AGENT_KEY" not in r["env"]


def test_v3_conf_carries_user_auth_and_priv(fake_snmp) -> None:
    mod, answer, calls = fake_snmp
    answer(_sys())
    _collect(mod, _V3, [])
    r = calls()[0]
    assert not any("Pass" in a or "monitor" in a for a in r["argv"])
    conf = r["conf"]
    for line in ("defVersion 3", "defSecurityName monitor", "defSecurityLevel authPriv",
                 "defAuthType SHA-256", "defAuthPassphrase auth-Pass-123", "defPrivType AES",
                 "defPrivPassphrase priv-Pass-456"):
        assert line in conf.splitlines(), line


def test_conf_values_with_quotes_blanks_or_hash_are_quoted() -> None:
    mod = _agent()
    assert mod._snmp_conf_value("plain") == "plain"
    assert mod._snmp_conf_value(" lead") == '" lead"'
    assert mod._snmp_conf_value('"q') == '"\\"q"'
    assert mod._snmp_conf_value("a#b") == '"a#b"'      # 沒加引號的話 net-snmp 會把 # 之後當成註解
    assert mod._snmp_conf_value("a\\b") == '"a\\\\b"'


@pytest.mark.parametrize("bad", [
    {**_V2C, "community": "x\nmibs +ALL"},
    {**_V2C, "community": "x\rdefVersion 1"},
    {**_V2C, "community": ""},
    {**_V3, "username": "a b"},
    {**_V3, "auth_protocol": "SHA-1024"},
    {**_V3, "priv_protocol": "3DES"},
    {**_V3, "security_level": "noAuthNoPriv"},
    {**_V3, "auth_passphrase": "x" * 200},
    {"version": "v1", "community": "public"},
])
def test_agent_rejects_unsafe_or_unknown_credentials(bad) -> None:
    assert _agent()._snmp_cred_ok(bad) is None


def test_budget_from_server_is_clamped() -> None:
    b = _agent()._snmp_budget({"target_seconds": 99999, "max_rows": 10**9, "max_repetitions": 500,
                               "pause_ms": -5, "walk_seconds": "x"})
    assert b["target_seconds"] == 600 and b["max_rows"] == 200000 and b["max_repetitions"] == 25
    assert b["pause_ms"] == 0 and b["walk_seconds"] == 120


def test_only_allowlisted_oids_are_ever_asked(fake_snmp) -> None:
    mod, answer, calls = fake_snmp
    s = mod._SnmpSession("192.0.2.1", 161, mod._snmp_cred_ok(_V2C), mod._snmp_budget(_BUDGET))
    rows, st, err = s.run("snmpbulkwalk", ["1.3.6.1.4.1.9.9.999.1"])
    assert st == "error" and "allowlist" in err and rows == []
    rows, st, err = s.run("snmpset", [mod.SNMP_OIDS["sysName"]])
    assert st == "error"
    assert calls() == []


# ─────────────────── 解析 ───────────────────
def test_parse_joins_wrapped_hex_and_drops_absent() -> None:
    mod = _agent()
    rows = mod._snmp_parse('.1.3.6.1.2.1.1.1.0 "4C 69 6E \n75 78 "\n.1.3.6.1.2.1.1.9.0 No Such Object available\n')
    assert rows[0] == ("1.3.6.1.2.1.1.1.0", '"4C 69 6E 75 78 "')
    assert mod._snmp_text(rows[0][1]) == "Linux"
    assert mod._snmp_absent(rows[1][1])


def test_arp_physical_table_keeps_state_and_type(fake_snmp) -> None:
    mod, answer, _calls = fake_snmp
    phys = "1.3.6.1.2.1.4.35.1"
    answer({**_sys(),
            f"{phys}.4": _lines(f"{phys}.4", [("5.1.4.192.0.2.10", _hb(0, 0x50, 0x56, 1, 2, 3)),
                                              ("5.1.4.192.0.2.11", _hb(0, 0x50, 0x56, 1, 2, 4)),
                                              ("6.2.16." + ".".join(["32", "1", "13", "184"] + ["0"] * 11 + ["9"]),
                                               _hb(0, 0x50, 0x56, 1, 2, 5))]),
            f"{phys}.7": _lines(f"{phys}.7", [("5.1.4.192.0.2.10", "1"), ("5.1.4.192.0.2.11", "2")]),
            f"{phys}.6": _lines(f"{phys}.6", [("5.1.4.192.0.2.10", "3"), ("5.1.4.192.0.2.11", "3")])})
    sec = _collect(mod, _V2C, ["arp"])["sections"]["arp"]
    assert sec["status"] == "ok" and sec["table"] == "physical"
    assert ["192.0.2.10", "00:50:56:01:02:03", 5, 1, 3] in sec["rows"]
    assert ["192.0.2.11", "00:50:56:01:02:04", 5, 2, 3] in sec["rows"]
    assert any(r[0] == "2001:db8::9" for r in sec["rows"])


def test_arp_falls_back_to_legacy_media_table(fake_snmp) -> None:
    mod, answer, _calls = fake_snmp
    media = "1.3.6.1.2.1.4.22.1"
    answer({**_sys(),
            f"{media}.2": _lines(f"{media}.2", [("3.198.51.100.7", _hb(0xaa, 0xbb, 0xcc, 0, 0, 1))]),
            f"{media}.4": _lines(f"{media}.4", [("3.198.51.100.7", "4")])})
    sec = _collect(mod, _V2C, ["arp"])["sections"]["arp"]
    assert sec["table"] == "media"
    assert sec["rows"] == [["198.51.100.7", "aa:bb:cc:00:00:01", 3, None, 4]]


def _ifnames() -> dict:
    return {"1.3.6.1.2.1.31.1.1.1.1": _lines("1.3.6.1.2.1.31.1.1.1.1", [("10101", _h("Gi1/0/1")),
                                                                        ("10102", _h("Gi1/0/2"))])}


def test_fdb_qbridge_maps_bridge_port_to_ifname_and_vlan(fake_snmp) -> None:
    mod, answer, _calls = fake_snmp
    q = "1.3.6.1.2.1.17.7.1.2.2.1.2"
    answer({**_sys(oid=".1.3.6.1.4.1.11.2.3.7.11.1"), **_ifnames(),
            q: _lines(q, [("10.0.80.86.1.2.3", "1"), ("20.0.80.86.1.2.4", "2"), ("10.0.80.86.9.9.9", "0")]),
            "1.3.6.1.2.1.17.1.4.1.2": _lines("1.3.6.1.2.1.17.1.4.1.2", [("1", "10101"), ("2", "10102")]),
            "1.3.6.1.2.1.17.7.1.4.2.1.3": _lines("1.3.6.1.2.1.17.7.1.4.2.1.3", [("0.100", "10"), ("0.200", "20")])})
    sec = _collect(mod, _V2C, ["fdb"])["sections"]["fdb"]
    assert sec["status"] == "ok"
    assert sorted(sec["rows"]) == [["00:50:56:01:02:03", "Gi1/0/1", 100], ["00:50:56:01:02:04", "Gi1/0/2", 200]]


def test_cisco_fdb_per_vlan_v2c_uses_community_at_vlan(fake_snmp) -> None:
    mod, answer, calls = fake_snmp
    bp, fdb = "1.3.6.1.2.1.17.1.4.1.2", "1.3.6.1.2.1.17.4.3.1.2"
    answer({**_sys(), **_ifnames(),
            "1.3.6.1.4.1.9.9.46.1.3.1.1.2": _lines("1.3.6.1.4.1.9.9.46.1.3.1.1.2",
                                                    [("1.1", "1"), ("1.30", "1"), ("1.1002", "1"), ("1.40", "2")]),
            f"@1|{bp}": _lines(bp, [("1", "10101")]),
            f"@1|{fdb}": _lines(fdb, [("0.80.86.1.1.1", "1")]),
            f"@30|{bp}": _lines(bp, [("5", "10102")]),
            f"@30|{fdb}": _lines(fdb, [("0.80.86.3.3.3", "5")])})
    sec = _collect(mod, _V2C, ["fdb"])["sections"]["fdb"]
    assert sorted(sec["rows"]) == [["00:50:56:01:01:01", "Gi1/0/1", 1], ["00:50:56:03:03:03", "Gi1/0/2", 30]]
    comms = {line for r in calls() for line in r["conf"].splitlines() if line.startswith("defCommunity")}
    assert "defCommunity s3cret-Comm!@30" in comms
    assert not any("@1002" in c or "@40" in c for c in comms), "不存在／非 operational 的 VLAN 不該去讀"
    assert not any("s3cret" in a for r in calls() for a in r["argv"])


def test_cisco_fdb_per_vlan_v3_uses_context(fake_snmp) -> None:
    mod, answer, calls = fake_snmp
    bp, fdb = "1.3.6.1.2.1.17.1.4.1.2", "1.3.6.1.2.1.17.4.3.1.2"
    answer({**_sys(), **_ifnames(),
            "1.3.6.1.4.1.9.9.46.1.3.1.1.2": _lines("1.3.6.1.4.1.9.9.46.1.3.1.1.2", [("1.30", "1")]),
            f"vlan-30|{bp}": _lines(bp, [("5", "10102")]),
            f"vlan-30|{fdb}": _lines(fdb, [("0.80.86.3.3.3", "5")])})
    sec = _collect(mod, _V3, ["fdb"])["sections"]["fdb"]
    assert sec["rows"] == [["00:50:56:03:03:03", "Gi1/0/2", 30]]
    assert any("-n" in r["argv"] and "vlan-30" in r["argv"] for r in calls())


def test_lldp_and_cdp_neighbors(fake_snmp) -> None:
    mod, answer, _calls = fake_snmp
    rem, loc, cdp = "1.0.8802.1.1.2.1.4.1.1", "1.0.8802.1.1.2.1.3.7.1", "1.3.6.1.4.1.9.9.23.1.2.1.1"
    answer({**_sys(), **_ifnames(),
            f"{rem}.9": _lines(f"{rem}.9", [("0.3.1", _h("dist-sw.example.net"))]),
            f"{rem}.5": _lines(f"{rem}.5", [("0.3.1", _hb(0x00, 0x11, 0x22, 0x33, 0x44, 0x55))]),
            f"{rem}.4": _lines(f"{rem}.4", [("0.3.1", "4")]),
            f"{rem}.7": _lines(f"{rem}.7", [("0.3.1", _h("Te1/1/1"))]),
            f"{rem}.6": _lines(f"{rem}.6", [("0.3.1", "5")]),
            f"{rem}.10": _lines(f"{rem}.10", [("0.3.1", _h("Cisco IOS XE\nsecond line"))]),
            "1.0.8802.1.1.2.1.4.2.1.3": _lines("1.0.8802.1.1.2.1.4.2.1.3", [("0.3.1.1.4.192.0.2.2", "2")]),
            f"{loc}.3": _lines(f"{loc}.3", [("3", _h("Gi1/0/3"))]),
            f"{loc}.2": _lines(f"{loc}.2", [("3", "5")]),
            f"{cdp}.6": _lines(f"{cdp}.6", [("10101.1", _h("ap-01"))]),
            f"{cdp}.7": _lines(f"{cdp}.7", [("10101.1", _h("GigabitEthernet0"))]),
            f"{cdp}.8": _lines(f"{cdp}.8", [("10101.1", _h("cisco AIR-AP2802I"))]),
            f"{cdp}.3": _lines(f"{cdp}.3", [("10101.1", "1")]),
            f"{cdp}.4": _lines(f"{cdp}.4", [("10101.1", _hb(192, 0, 2, 50))])})
    sec = _collect(mod, _V2C, ["neighbors"])["sections"]["neighbors"]
    assert sec["status"] == "ok"
    lldp = next(r for r in sec["rows"] if r["protocol"] == "lldp")
    assert lldp == {"protocol": "lldp", "local_port": "Gi1/0/3", "remote_sysname": "dist-sw.example.net",
                    "remote_port": "Te1/1/1", "remote_chassis_id": "00:11:22:33:44:55",
                    "remote_mac": "00:11:22:33:44:55", "remote_mgmt_address": "192.0.2.2",
                    "remote_platform": "Cisco IOS XE"}
    cdp_row = next(r for r in sec["rows"] if r["protocol"] == "cdp")
    assert cdp_row["local_port"] == "Gi1/0/1" and cdp_row["remote_sysname"] == "ap-01"
    assert cdp_row["remote_mgmt_address"] == "192.0.2.50" and cdp_row["remote_platform"] == "cisco AIR-AP2802I"


def test_interfaces_and_unicast_addresses(fake_snmp) -> None:
    mod, answer, _calls = fake_snmp
    answer({**_sys(),
            "1.3.6.1.2.1.2.2.1.2": _lines("1.3.6.1.2.1.2.2.1.2", [("1", _h("GigabitEthernet1/0/1")),
                                                                  ("100", _h("Vlan100"))]),
            "1.3.6.1.2.1.31.1.1.1.1": _lines("1.3.6.1.2.1.31.1.1.1.1", [("1", _h("Gi1/0/1")), ("100", _h("Vl100"))]),
            "1.3.6.1.2.1.31.1.1.1.18": _lines("1.3.6.1.2.1.31.1.1.1.18", [("1", _h("to-core"))]),
            "1.3.6.1.2.1.2.2.1.3": _lines("1.3.6.1.2.1.2.2.1.3", [("1", "6"), ("100", "53")]),
            "1.3.6.1.2.1.2.2.1.6": _lines("1.3.6.1.2.1.2.2.1.6", [("1", _hb(0, 0x11, 0x22, 0x33, 0x44, 0x01))]),
            "1.3.6.1.2.1.2.2.1.7": _lines("1.3.6.1.2.1.2.2.1.7", [("1", "1"), ("100", "1")]),
            "1.3.6.1.2.1.2.2.1.8": _lines("1.3.6.1.2.1.2.2.1.8", [("1", "1"), ("100", "1")]),
            "1.3.6.1.2.1.4.34.1.3": _lines("1.3.6.1.2.1.4.34.1.3", [("1.4.192.0.2.1", "100"),
                                                                    ("1.4.192.0.2.255", "100")]),
            "1.3.6.1.2.1.4.34.1.4": _lines("1.3.6.1.2.1.4.34.1.4", [("1.4.192.0.2.1", "1"),
                                                                    ("1.4.192.0.2.255", "3")])})
    sec = _collect(mod, _V2C, ["interfaces"])["sections"]["interfaces"]
    assert sec["status"] == "ok"
    gi = next(r for r in sec["rows"] if r["if_index"] == 1)
    assert gi == {"if_index": 1, "name": "Gi1/0/1", "descr": "GigabitEthernet1/0/1", "alias": "to-core",
                  "type": 6, "mac": "00:11:22:33:44:01", "admin": 1, "oper": 1}
    assert sec["addresses"] == [{"ip": "192.0.2.1", "if_index": 100}]     # 廣播位址不算


def test_row_cap_stops_and_marks_partial(fake_snmp) -> None:
    mod, answer, _calls = fake_snmp
    media = "1.3.6.1.2.1.4.22.1.2"
    rows = [(f"3.10.0.{i // 250}.{i % 250 + 1}", _hb(0, 0x50, 0x56, 0, i // 250, i % 250)) for i in range(400)]
    answer({**_sys(), media: _lines(media, rows)})
    sec = _collect(mod, _V2C, ["arp"], {"max_rows": 100})["sections"]["arp"]
    assert sec["status"] == "partial" and sec["truncated"] is True
    assert len(sec["rows"]) <= 100


def test_unreachable_device_reports_the_reason(fake_snmp) -> None:
    mod, answer, _calls = fake_snmp
    answer({"1.3.6.1.2.1.1.1.0": {"exit": 1, "stderr": "Timeout: No Response from udp:192.0.2.1:161."}})
    out = _collect(mod, _V2C, ["arp", "fdb"])
    assert out["error"].startswith("Timeout") and out["error_code"] == "snmp_unreachable"
    assert out["sections"] == {}


# ─────────────────── 工作與排程 ───────────────────
def test_snmp_jobs_are_allowlisted_and_validated_by_the_agent(fake_snmp) -> None:
    mod, answer, _calls = fake_snmp
    assert "snmp_test" in mod._JOB_KINDS and "snmp_collect" in mod._JOB_KINDS
    _r, err = mod._job_execute("snmp_walk_anything", {"ip": "192.0.2.1"})
    assert err and "unsupported" in err
    _r, err = mod._job_execute("snmp_test", {"ip": "evil.example.com", "credentials": [_V2C]})
    assert err == "invalid snmp target"
    _r, err = mod._job_execute("snmp_test", {"ip": "192.0.2.1", "credentials": [{**_V2C, "community": "a\nb"}]})
    assert err == "invalid snmp credential"
    answer(_sys())
    res, err = mod._job_execute("snmp_test", {"ip": "192.0.2.1", "port": 161, "sections": {},
                                              "credentials": [_V2C, _V3], "budget": _BUDGET})
    assert err is None
    assert [i["ok"] for i in res["items"]] == [True, True]
    assert res["items"][0]["system"]["sys_name"] == "core-sw"
    assert "community" not in json.dumps(res) and "s3cret" not in json.dumps(res)


def test_capability_reported_only_with_netsnmp_tools(monkeypatch) -> None:
    mod = _agent()
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert "snmp" not in mod._capabilities()
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}" if name.startswith("snmp") else None)
    assert "snmp" in mod._capabilities()
    assert "snmpbulkwalk|1|" in mod._tools_header()


def test_assignment_from_poll_and_schedule() -> None:
    mod = _agent()
    good = {"target_id": "t1", "ip": "192.0.2.1", "port": 161, "sections": {"arp": 300, "fdb": 30, "bogus": 5},
            "credential": _V2C}
    bad = {"target_id": "t2", "ip": "192.0.2.2", "sections": {"arp": 300}, "credential": {**_V2C, "community": "a\nb"}}
    mod._snmp_set_assigned([good, bad, "junk"], {"pause_ms": 0})
    assert [t["target_id"] for t in mod._SNMP_STATE["targets"]] == ["t1"]
    t = mod._SNMP_STATE["targets"][0]
    assert t["sections"] == {"arp": 300, "fdb": 60}           # 未知區段丟掉、間隔下限 60 秒
    due = mod._snmp_due(1000.0)
    assert [sorted(names) for _t, names, _b in due] == [["arp", "fdb"]]
    assert mod._snmp_due(1030.0) == []
    assert [names for _t, names, _b in mod._snmp_due(1061.0)] == [["fdb"]]
    mod._snmp_set_assigned([], {})                             # 不再指派：狀態與上次時間一起丟掉
    assert mod._SNMP_STATE["targets"] == [] and not mod._SNMP_LAST


def test_conf_files_are_private(fake_snmp) -> None:
    mod, answer, calls = fake_snmp
    answer(_sys())
    _collect(mod, _V2C, [])
    r = calls()[0]
    assert stat.S_IMODE(r["mode"]) & 0o077 == 0
