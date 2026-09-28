"""IP 詳細頁的「探測」：由負責該子網路的掃描代理對單一 IP 做非侵入式的識別
（服務版本、OS 指紋、banner／TLS 憑證、名稱查詢），推出這是什麼主機（2026-09-28 使用者要求）。

- 只有管理員能用；每次都寫稽核
- 目標只能是 jt-ipam 裡已有的單一 IP（不接受主機名稱、不接受多個目標）—— 後端與代理各驗一次
- 由那個子網路的掃描代理執行；沒有代理負責時講清楚
- 同一個 IP 同時只能跑一個探測
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.models.address import IPAddress
from app.models.agent_probe_job import STATUS_DONE, STATUS_PENDING, AgentProbeJob
from app.models.scan_agent import ScanAgent
from app.models.section import Section
from app.models.subnet import Subnet
from app.services.agent_probe import ProbeJobError, validate_params
from sqlalchemy import select

# ─────────────────── 參數驗證（後端） ───────────────────

def test_identify_takes_one_ip() -> None:
    assert validate_params("identify", {"targets": "198.51.100.7"}) == {"targets": ["198.51.100.7"]}


@pytest.mark.parametrize("targets", ["host.example.net", "198.51.100.7 198.51.100.8", "198.51.100.0/24", ""])
def test_identify_rejects_anything_but_a_single_ip(targets: str) -> None:
    with pytest.raises(ProbeJobError):
        validate_params("identify", {"targets": targets})


# ─────────────────── 端點 ───────────────────

async def _setup(db, *, with_agent: bool = True):
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    agent = None
    if with_agent:
        agent = ScanAgent(name=f"agent-{uuid.uuid4().hex[:6]}", enroll_key_hash="x" * 64, enabled=True)
        db.add(agent)
        await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24", scan_agent_id=agent.id if agent else None)
    db.add(sub)
    await db.flush()
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.7", state="active")
    db.add(ip)
    await db.commit()
    return ip, agent


async def test_identify_creates_a_job_for_the_subnets_agent(client, auth_headers, db_session) -> None:
    ip, agent = await _setup(db_session)
    r = await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["agent_name"] == agent.name
    job = await db_session.get(AgentProbeJob, uuid.UUID(body["job_id"]))
    assert job.kind == "identify"
    assert job.params == {"targets": ["198.51.100.7"]}
    assert job.agent_id == agent.id
    assert job.status == STATUS_PENDING

    from app.models.audit import AuditLog
    audit = (await db_session.execute(select(AuditLog).where(
        AuditLog.action == "identify", AuditLog.object_id == ip.id))).scalars().first()
    assert audit is not None


async def test_only_one_probe_per_ip_at_a_time(client, auth_headers, db_session) -> None:
    ip, _ = await _setup(db_session)
    r1 = await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)
    assert r1.status_code == 202, r1.text
    r2 = await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"]["code"] == "identify_in_progress"


async def test_a_subnet_without_a_scan_agent_says_so(client, auth_headers, db_session) -> None:
    ip, _ = await _setup(db_session, with_agent=False)
    r = await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "identify_no_agent"


async def test_non_admins_cannot_probe(client, db_session) -> None:
    from app.core.security import hash_password
    from app.models.user import User
    from app.services.auth import issue_access_token
    ip, _ = await _setup(db_session)
    u = User(username=f"viewer-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@e.test",
             password_hash=hash_password("Xx!12345678xX"), is_admin=False, is_active=True)
    db_session.add(u)
    await db_session.commit()
    h = {"Authorization": f"Bearer {issue_access_token(u)}"}
    r = await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=h)
    assert r.status_code == 403, r.text


async def test_result_comes_back_with_a_summary(client, auth_headers, db_session) -> None:
    ip, _ = await _setup(db_session)
    r = await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)
    job_id = r.json()["job_id"]
    job = await db_session.get(AgentProbeJob, uuid.UUID(job_id))
    job.status = STATUS_DONE
    job.result = SAMPLE_RESULT
    await db_session.commit()

    got = await client.get(f"/api/v1/addresses/{ip.id}/identify/{job_id}", headers=auth_headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == STATUS_DONE
    assert body["summary"]["device_type"] == "server"
    assert body["summary"]["os"] == "Linux 5.0 - 6.2"
    assert "22/tcp ssh OpenSSH 9.6p1" in body["summary"]["services"]

    # 最近一次的結果：重新打開畫面時看得到
    latest = await client.get(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)
    assert latest.status_code == 200
    assert latest.json()["job_id"] == job_id


async def test_a_job_of_another_ip_is_not_returned(client, auth_headers, db_session) -> None:
    ip, agent = await _setup(db_session)
    other = AgentProbeJob(agent_id=agent.id, kind="identify", params={"targets": ["198.51.100.99"]},
                          status=STATUS_DONE, result=SAMPLE_RESULT,
                          expires_at=datetime.now(UTC))
    db_session.add(other)
    await db_session.commit()
    got = await client.get(f"/api/v1/addresses/{ip.id}/identify/{other.id}", headers=auth_headers)
    assert got.status_code == 404


# ─────────────────── 摘要（由證據推出類型／廠牌） ───────────────────

SAMPLE_RESULT = {
    "target": "198.51.100.7",
    "names": {"rdns": "srv-01.example.net", "netbios": None, "mdns": None},
    "nmap": {
        "available": True,
        "mac": "00:00:5E:00:53:01", "mac_vendor": "ICANN, IANA Department",
        "os": [{"name": "Linux 5.0 - 6.2", "accuracy": 96, "type": "general purpose",
                "vendor": "Linux", "family": "Linux"}],
        "ports": [
            {"port": 22, "proto": "tcp", "state": "open", "service": "ssh", "product": "OpenSSH",
             "version": "9.6p1", "extrainfo": "Ubuntu Linux; protocol 2.0", "scripts": {}},
            {"port": 443, "proto": "tcp", "state": "open", "service": "https", "product": "nginx",
             "version": "1.24.0", "extrainfo": "", "tunnel": "ssl",
             "scripts": {"http-title": "Welcome", "ssl-cert": "Subject: commonName=srv-01.example.net"}},
        ],
    },
}


def test_summary_of_a_linux_server() -> None:
    from app.services import ip_identify
    s = ip_identify.summarize(SAMPLE_RESULT, mac_vendor="IANA")
    assert s["device_type"] == "server"
    assert s["os"] == "Linux 5.0 - 6.2"
    assert s["vendor"] == "IANA"
    assert "srv-01.example.net" in s["names"]
    assert s["services"] == ["22/tcp ssh OpenSSH 9.6p1", "443/tcp https nginx 1.24.0"]


@pytest.mark.parametrize(("ports", "osclass", "expected"), [
    ([{"port": 9100, "service": "jetdirect"}], None, "printer"),
    ([{"port": 631, "service": "ipp"}], None, "printer"),
    ([{"port": 554, "service": "rtsp"}], None, "camera"),
    ([{"port": 5060, "service": "sip"}], None, "voip"),
    ([{"port": 3389, "service": "ms-wbt-server"}], None, "windows"),
    ([{"port": 8006, "service": "https", "product": "Proxmox Virtual Environment REST API"}], None, "hypervisor"),
    ([], "router", "router"),
    ([], "switch", "switch"),
    ([], "WAP", "wireless_ap"),
    ([], "printer", "printer"),
    ([], None, "unknown"),
])
def test_device_type_rules(ports, osclass, expected) -> None:
    from app.services import ip_identify
    res = {"nmap": {"available": True, "os": [{"name": "x", "accuracy": 90, "type": osclass}] if osclass else [],
                    "ports": [{"proto": "tcp", "state": "open", **p} for p in ports]}}
    assert ip_identify.summarize(res)["device_type"] == expected


def test_summary_when_the_agent_has_no_nmap() -> None:
    from app.services import ip_identify
    s = ip_identify.summarize({"names": {"rdns": "a.example.net"}, "nmap": {"available": False}})
    assert s["device_type"] == "unknown"
    assert s["nmap_available"] is False
    assert s["names"] == ["a.example.net"]


def test_names_from_a_cert_skip_the_issuer_and_wildcards() -> None:
    """正式環境實測：Let's Encrypt 簽發者的 CN（YR2）與萬用憑證 *.example.net 都被當成主機名稱。"""
    from app.services import ip_identify
    cert = ("Subject: commonName=*.example.net\n"
            "Subject Alternative Name: DNS:*.example.net, DNS:pve-01.example.net\n"
            "Issuer: commonName=YR2/organizationName=Let's Encrypt/countryName=US\n"
            "Public Key type: ec")
    res = {"nmap": {"available": True, "ports": [
        {"port": 8006, "proto": "tcp", "state": "open", "service": "https", "scripts": {"ssl-cert": cert}}]}}
    assert ip_identify.summarize(res)["names"] == ["pve-01.example.net"]


def test_a_cert_with_many_names_does_not_flood_the_list() -> None:
    from app.services import ip_identify
    sans = ", ".join(f"DNS:site{i}.example.net" for i in range(20))
    cert = f"Subject: commonName=site0.example.net\nSubject Alternative Name: {sans}\nIssuer: commonName=CA"
    res = {"nmap": {"available": True, "ports": [
        {"port": 443, "proto": "tcp", "state": "open", "service": "https", "scripts": {"ssl-cert": cert}}]}}
    assert len(ip_identify.summarize(res)["names"]) <= 3


# ─────────────────── 代理端（後端被入侵時的最後一道閘） ───────────────────

def _agent_module():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / "agent" / "jt_ipam_agent.py"
    spec = importlib.util.spec_from_file_location("jt_agent_identify_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("targets", [["host.example.net"], ["198.51.100.7", "198.51.100.8"], ["-oX=/tmp/x"]])
def test_agent_refuses_identify_on_anything_but_one_ip(targets) -> None:
    mod = _agent_module()
    result, error = mod._job_execute("identify", {"targets": targets})
    assert result is None
    assert error


NMAP_XML = """<?xml version="1.0"?>
<nmaprun scanner="nmap" args="nmap -Pn -sV" start="1790560000" version="7.94">
<host starttime="1790560000" endtime="1790560030"><status state="up" reason="user-set"/>
<address addr="198.51.100.7" addrtype="ipv4"/>
<address addr="00:00:5E:00:53:01" addrtype="mac" vendor="ICANN, IANA Department"/>
<hostnames><hostname name="srv-01.example.net" type="PTR"/></hostnames>
<ports>
<port protocol="tcp" portid="22"><state state="open" reason="syn-ack"/>
<service name="ssh" product="OpenSSH" version="9.6p1" extrainfo="Ubuntu Linux; protocol 2.0" ostype="Linux" method="probed" conf="10"><cpe>cpe:/a:openbsd:openssh:9.6p1</cpe></service>
<script id="ssh-hostkey" output="&#xa;  256 aa:bb (ECDSA)&#xa;"/></port>
<port protocol="tcp" portid="443"><state state="open" reason="syn-ack"/>
<service name="http" product="nginx" version="1.24.0" tunnel="ssl" method="probed" conf="10"/>
<script id="http-title" output="Welcome"/>
<script id="ssl-cert" output="Subject: commonName=srv-01.example.net"/></port>
<port protocol="tcp" portid="80"><state state="closed" reason="reset"/><service name="http" method="table" conf="3"/></port>
</ports>
<os><osmatch name="Linux 5.0 - 6.2" accuracy="96" line="1">
<osclass type="general purpose" vendor="Linux" osfamily="Linux" osgen="5.X" accuracy="96"/></osmatch></os>
</host></nmaprun>"""


def test_agent_parses_nmap_xml() -> None:
    mod = _agent_module()
    out = mod._parse_nmap_xml(NMAP_XML)
    assert out["mac"] == "00:00:5E:00:53:01"
    assert out["mac_vendor"] == "ICANN, IANA Department"
    assert out["hostnames"] == ["srv-01.example.net"]
    assert [p["port"] for p in out["ports"]] == [22, 443]      # 只列開著的
    ssh = out["ports"][0]
    assert (ssh["service"], ssh["product"], ssh["version"]) == ("ssh", "OpenSSH", "9.6p1")
    assert out["ports"][1]["tunnel"] == "ssl"
    assert out["ports"][1]["scripts"]["http-title"] == "Welcome"
    assert out["os"][0] == {"name": "Linux 5.0 - 6.2", "accuracy": 96, "type": "general purpose",
                            "vendor": "Linux", "family": "Linux"}


def test_agent_parse_survives_garbage() -> None:
    mod = _agent_module()
    assert mod._parse_nmap_xml("not xml")["ports"] == []


async def test_the_tools_page_cannot_start_an_identify_on_any_address(client, auth_headers, db_session) -> None:
    """工具頁的代理探測可以打任意位址；identify 只能從 IP 詳細頁對 jt-ipam 裡的 IP 發起。"""
    agent = ScanAgent(name=f"agent-{uuid.uuid4().hex[:6]}", enroll_key_hash="y" * 64, enabled=True)
    db_session.add(agent)
    await db_session.commit()
    r = await client.post("/api/v1/tools/net/agent-probe", headers=auth_headers,
                          json={"agent_id": str(agent.id), "kind": "identify", "targets": "203.0.113.9"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "identify_use_ip_page"
    assert (await db_session.execute(select(AgentProbeJob))).scalars().first() is None


NMAP_SERVICES = """# comment line
tcpmux\t1/tcp\t0.001995
ssh\t22/tcp\t0.182286\t# Secure Shell
domain\t53/udp\t0.213496
http\t80/tcp\t0.484143
https\t443/tcp\t0.208669
telnet\t23/tcp\t0.221265
broken line
"""


def test_agent_port_list_is_top_ports_plus_infrastructure_ports(tmp_path) -> None:
    """nmap 的前 N 個常用埠不含 8006（PVE）這類基礎設施埠，而 -p 與 --top-ports 併用時是取交集，
    所以代理自己從 nmap-services 算前 N 個再併上補充清單。"""
    mod = _agent_module()
    f = tmp_path / "nmap-services"
    f.write_text(NMAP_SERVICES)
    ports = [int(x) for x in mod._identify_port_list(str(f), top=3).split(",")]
    assert {80, 23, 443} <= set(ports)                     # 依頻率取前 3 個（只算 TCP）
    assert not {22, 53, 1} & set(ports)
    assert {8006, 5985} <= set(ports)                      # 補充的基礎設施埠
    assert ports == sorted(set(ports))


def test_agent_port_list_falls_back_when_nmap_services_is_missing(tmp_path) -> None:
    mod = _agent_module()
    assert mod._identify_port_list(str(tmp_path / "nope")) is None


# ─────────────────── 第二版：深度、進度、歷次結果、應用程式（2026-09-28 使用者回饋） ───────────────────

async def _agent_with_key(db, raw_key: str):
    from app.api.v1.endpoints.scan_agents import _key_hash
    ip, agent = await _setup(db)
    agent.enroll_key_hash = _key_hash(raw_key)
    await db.commit()
    return ip, agent


async def test_agent_reports_progress_and_the_page_sees_it(client, auth_headers, db_session) -> None:
    raw = "p" * 40
    ip, _ = await _agent_with_key(db_session, raw)
    job_id = (await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)).json()["job_id"]
    got = await client.get("/api/v1/scan-agents/jobs", headers={"X-Agent-Key": raw})
    assert [j["id"] for j in got.json()["jobs"]] == [job_id]

    prog = {"stage": "tcp", "elapsed": 12}
    r = await client.post(f"/api/v1/scan-agents/jobs/{job_id}/progress", json={"progress": prog},
                          headers={"X-Agent-Key": raw})
    assert r.status_code == 200, r.text
    body = (await client.get(f"/api/v1/addresses/{ip.id}/identify/{job_id}", headers=auth_headers)).json()
    assert body["status"] == "running"
    assert body["progress"]["stage"] == "tcp"


async def test_progress_is_only_accepted_from_the_agent_running_the_job(client, auth_headers, db_session) -> None:
    raw = "q" * 40
    ip, _ = await _agent_with_key(db_session, raw)
    job_id = (await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)).json()["job_id"]
    # 還沒被領走（不是 running）→ 拒絕
    r = await client.post(f"/api/v1/scan-agents/jobs/{job_id}/progress", json={"progress": {"stage": "tcp"}},
                          headers={"X-Agent-Key": raw})
    assert r.status_code == 404
    await client.get("/api/v1/scan-agents/jobs", headers={"X-Agent-Key": raw})
    # 別的代理 → 拒絕
    from app.api.v1.endpoints.scan_agents import _key_hash
    other = ScanAgent(name=f"agent-{uuid.uuid4().hex[:6]}", enroll_key_hash=_key_hash("o" * 40), enabled=True)
    db_session.add(other)
    await db_session.commit()
    r = await client.post(f"/api/v1/scan-agents/jobs/{job_id}/progress", json={"progress": {"stage": "tcp"}},
                          headers={"X-Agent-Key": "o" * 40})
    assert r.status_code == 404
    # 太大 → 拒絕（進度只是幾行狀態，不是結果）
    r = await client.post(f"/api/v1/scan-agents/jobs/{job_id}/progress",
                          json={"progress": {"log": ["x" * 1000] * 40}}, headers={"X-Agent-Key": raw})
    assert r.status_code == 413


async def test_history_lists_this_ips_probes_newest_first(client, auth_headers, db_session) -> None:
    ip, agent = await _setup(db_session)
    base = datetime.now(UTC)
    from datetime import timedelta
    for i in range(3):
        db_session.add(AgentProbeJob(
            agent_id=agent.id, kind="identify", params={"targets": ["198.51.100.7"]},
            status=STATUS_DONE, result=SAMPLE_RESULT, expires_at=base,
            created_at=base - timedelta(hours=3 - i)))
    db_session.add(AgentProbeJob(agent_id=agent.id, kind="identify", params={"targets": ["198.51.100.99"]},
                                 status=STATUS_DONE, result=SAMPLE_RESULT, expires_at=base))
    await db_session.commit()
    r = await client.get(f"/api/v1/addresses/{ip.id}/identify/history", headers=auth_headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 3
    assert items[0]["created_at"] > items[1]["created_at"] > items[2]["created_at"]
    assert items[0]["summary"]["device_type"] == "server"      # 清單上就看得出是什麼
    assert "result" not in items[0]                             # 清單不帶整包原始結果


def test_a_cert_name_that_is_not_a_host_name_is_not_a_name() -> None:
    """實測：7070 埠的憑證 CN 是「AnyDesk Client」，被當成主機名稱「AnyDesk」。"""
    from app.services import ip_identify
    cert = "Subject: commonName=AnyDesk Client\nIssuer: commonName=AnyDesk Client\nPublic Key type: rsa"
    res = {"nmap": {"available": True, "ports": [
        {"port": 7070, "proto": "tcp", "state": "open", "service": "realserver", "tunnel": "ssl",
         "scripts": {"ssl-cert": cert}}]}}
    s = ip_identify.summarize(res)
    assert s["names"] == []
    assert "AnyDesk" in s["applications"]          # 但它說明了這一埠跑的是什麼軟體


def test_applications_come_from_products_and_cert_hints() -> None:
    from app.services import ip_identify
    res = {"nmap": {"available": True, "ports": [
        {"port": 22, "proto": "tcp", "state": "open", "service": "ssh", "product": "OpenSSH", "version": "8.9p1"},
        {"port": 80, "proto": "tcp", "state": "open", "service": "http", "product": "Apache httpd", "version": "2.4.52"},
        {"port": 4000, "proto": "tcp", "state": "open", "service": "nomachine-nx",
         "product": "NoMachine NX Server remote desktop", "version": "7.9.2"},
        {"port": 443, "proto": "tcp", "state": "open", "service": "https", "product": "nginx"},
        {"port": 8443, "proto": "tcp", "state": "open", "service": "https", "product": "nginx"},
    ]}}
    apps = ip_identify.summarize(res)["applications"]
    assert apps[:3] == ["OpenSSH 8.9p1", "Apache httpd 2.4.52", "NoMachine NX Server remote desktop 7.9.2"]
    assert apps.count("nginx") == 1                  # 同一個軟體開兩個埠只列一次




async def test_a_result_shows_what_changed_since_the_previous_probe(client, auth_headers, db_session) -> None:
    """歷次結果都留著，所以每一筆都能跟上一筆比：新開、關掉、版本變了的服務。"""
    ip, agent = await _setup(db_session)
    from datetime import timedelta
    base = datetime.now(UTC)
    old = {"nmap": {"available": True, "ports": [
        {"port": 22, "proto": "tcp", "state": "open", "service": "ssh", "product": "OpenSSH", "version": "8.9p1"},
        {"port": 80, "proto": "tcp", "state": "open", "service": "http", "product": "Apache httpd"}]}}
    new = {"nmap": {"available": True, "ports": [
        {"port": 22, "proto": "tcp", "state": "open", "service": "ssh", "product": "OpenSSH", "version": "9.6p1"},
        {"port": 443, "proto": "tcp", "state": "open", "service": "https", "product": "nginx"}]}}
    j1 = AgentProbeJob(agent_id=agent.id, kind="identify", params={"targets": ["198.51.100.7"]},
                       status=STATUS_DONE, result=old, expires_at=base, created_at=base - timedelta(days=1))
    j2 = AgentProbeJob(agent_id=agent.id, kind="identify", params={"targets": ["198.51.100.7"]},
                       status=STATUS_DONE, result=new, expires_at=base, created_at=base)
    db_session.add_all([j1, j2])
    await db_session.commit()
    body = (await client.get(f"/api/v1/addresses/{ip.id}/identify/{j2.id}", headers=auth_headers)).json()
    ch = body["changes"]
    assert ch["previous_job_id"] == str(j1.id)
    assert ch["opened"] == ["443/tcp"]
    assert ch["closed"] == ["80/tcp"]
    assert ch["changed"] == [{"port": "22/tcp", "before": "OpenSSH 8.9p1", "after": "OpenSSH 9.6p1"}]
    first = (await client.get(f"/api/v1/addresses/{ip.id}/identify/{j1.id}", headers=auth_headers)).json()
    assert first["changes"] is None           # 第一次探測沒有可以比的


def test_agent_reports_which_stage_the_probe_is_in(monkeypatch) -> None:
    """探測要跑幾分鐘，畫面要看得到現在在做什麼：代理在每個階段開始時回報。"""
    mod = _agent_module()
    monkeypatch.setattr(mod, "_rdns", lambda ip: ("h.example.net", None))
    monkeypatch.setattr(mod, "_netbios", lambda ip: None)
    monkeypatch.setattr(mod, "_mdns", lambda ip: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)       # 沒有 nmap：只查名稱
    seen: list[str] = []
    result, error = mod._job_execute("identify", {"targets": ["198.51.100.7"]},
                                     progress=lambda p: seen.append(p["stage"]))
    assert error is None
    assert seen == ["names"]
    assert result["names"]["rdns"] == "h.example.net"


def test_agent_runs_identify_off_the_job_queue_thread() -> None:
    """探測要跑好幾分鐘；在工作佇列的執行緒上跑，這段期間別的工具探測會排不到而作廢。"""
    mod = _agent_module()
    assert mod._job_runs_in_background("identify") is True
    assert mod._job_runs_in_background("ping") is False


def test_a_linux_host_running_cups_is_not_a_printer() -> None:
    """實測：一台 Ubuntu 開發機開了 631/ipp（CUPS）被判成印表機。CUPS 是 Linux 的列印服務。"""
    from app.services import ip_identify
    res = {"nmap": {"available": True,
                    "os": [{"name": "Linux 5.0 - 5.4", "accuracy": 100, "type": "general purpose"}],
                    "ports": [
                        {"port": 22, "proto": "tcp", "state": "open", "service": "ssh", "product": "OpenSSH"},
                        {"port": 631, "proto": "tcp", "state": "open", "service": "ipp", "product": "CUPS",
                         "version": "2.4"}]}}
    assert ip_identify.summarize(res)["device_type"] == "server"


def test_a_real_printer_is_still_a_printer() -> None:
    from app.services import ip_identify
    res = {"nmap": {"available": True, "ports": [
        {"port": 631, "proto": "tcp", "state": "open", "service": "ipp", "product": "HP LaserJet ipp"},
    ]}}
    assert ip_identify.summarize(res)["device_type"] == "printer"


# ─────────────────── 探測出現在「作業」頁，完成時通知發起人（2026-09-28 使用者要求） ───────────────────

async def _task_of(db, job_id):
    from app.models.background_task import BackgroundTask
    return (await db.execute(select(BackgroundTask).where(
        BackgroundTask.kind == "ip.identify",
        BackgroundTask.summary["job_id"].astext == str(job_id)))).scalars().first()


async def test_a_probe_shows_up_as_a_task_and_notifies_when_done(client, auth_headers, db_session, admin_user) -> None:
    from app.models.notification import Notification
    raw = "t" * 40
    ip, agent = await _agent_with_key(db_session, raw)
    job_id = (await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)).json()["job_id"]
    task = await _task_of(db_session, job_id)
    assert task is not None
    assert (task.status, task.target_id, task.actor_user_id) == ("pending", ip.id, admin_user.id)
    assert "198.51.100.7" in task.target_label

    await client.get("/api/v1/scan-agents/jobs", headers={"X-Agent-Key": raw})
    await db_session.refresh(task)
    assert task.status == "running"
    await client.post(f"/api/v1/scan-agents/jobs/{job_id}/progress", json={"progress": {"stage": "scan"}},
                      headers={"X-Agent-Key": raw})
    await db_session.refresh(task)
    assert task.progress == 60

    r = await client.post(f"/api/v1/scan-agents/jobs/{job_id}/result", json={"result": SAMPLE_RESULT},
                          headers={"X-Agent-Key": raw})
    assert r.status_code == 200, r.text
    await db_session.refresh(task)
    assert (task.status, task.progress) == ("succeeded", 100)
    assert task.summary["device_type"] == "server"
    note = (await db_session.execute(select(Notification).where(
        Notification.user_id == admin_user.id, Notification.title_key == "notif.identify_done"))).scalars().first()
    assert note is not None
    assert note.link == f"/addresses/{ip.id}/identify?job={job_id}"
    assert note.params["ip"] == "198.51.100.7"
    assert note.params["type_key"] == "identify.type.server"


async def test_a_failed_probe_fails_the_task_and_says_so(client, auth_headers, db_session, admin_user) -> None:
    from app.models.notification import Notification
    raw = "u" * 40
    ip, _ = await _agent_with_key(db_session, raw)
    job_id = (await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)).json()["job_id"]
    await client.get("/api/v1/scan-agents/jobs", headers={"X-Agent-Key": raw})
    await client.post(f"/api/v1/scan-agents/jobs/{job_id}/result", json={"result": None, "error": "nmap crashed"},
                      headers={"X-Agent-Key": raw})
    task = await _task_of(db_session, job_id)
    assert task.status == "failed"
    assert task.error == "nmap crashed"
    assert (await db_session.execute(select(Notification).where(
        Notification.user_id == admin_user.id, Notification.title_key == "notif.identify_failed"))).scalars().first()


async def test_a_probe_nobody_picked_up_fails_its_task(client, auth_headers, db_session) -> None:
    from datetime import timedelta
    ip, _ = await _setup(db_session)
    job_id = (await client.post(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)).json()["job_id"]
    job = await db_session.get(AgentProbeJob, uuid.UUID(job_id))
    job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await client.get(f"/api/v1/addresses/{ip.id}/identify/history", headers=auth_headers)   # 會順手收掉過期的
    task = await _task_of(db_session, job_id)
    await db_session.refresh(task)
    assert task.status == "failed"
