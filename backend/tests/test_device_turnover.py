"""IP 換給另一台設備（MAC 變了）：從上一台推導出來的資料要清掉（使用者 2026-10-12）。

實際遇到的樣子：一個位址由 DHCP 先後發給 iPhone、iPad、MacBook，之後換成一台 Proxmox VM（MAC 不同）。
IP 頁仍顯示 OS＝macOS（os_guess 是很久以前 nmap 對上一台的猜測），因為 MAC 換了只會清名稱、不清辨識結果，
新的 VM 又擋掉所有探測，舊值永遠不會被蓋掉。

清的是「描述上一台設備、由偵測推導」的資料：OS 猜測／類別、設備類型／型號／辨識時間、OCS 盤點資料（照 MAC 對，
MAC 換了必然是上一台的）、跟著設備走的名稱（觀測與逐來源回報）。人填的（擁有者、說明、備註、裝置連結…）不動。
每一條會改 MAC 的路徑都要做：同步（ARP／租約／掃描代理）、手動編輯、MCP、phpIPAM 相容 API。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.address import IPAddress
from app.models.ip_change_log import IPChangeLog
from app.models.ip_hostname import IPHostnameObservation, IPHostnameReport
from app.models.section import Section
from app.models.subnet import Subnet
from app.services.arp_precedence import consider_mac
from sqlalchemy import select

pytestmark = pytest.mark.anyio

OLD, NEW = "00:00:5e:00:53:46", "00:00:5e:00:53:f8"


async def _ip(db_session, **kw):  # type: ignore[no-untyped-def]
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    then = datetime.now(UTC) - timedelta(days=40)
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.162", mac=OLD, mac_source="opnsense", hostname="laptop-07",
                   os_family="macos", os_guess="Apple Mac OS X 10.5.4 (Leopard)", device_kind="workstation",
                   device_model="MacBook Pro", device_identified_at=then,
                   os_ocs="macOS 14.5", ocs_id=42, ocs_tag="HQ", ocs_agent="OCS-NG_unified_unix_agent_v2.10",
                   ocs_notes=[{"k": "v"}], ocs_hw={"serial": "C02X"}, last_seen_ocs=then,
                   owner="IT", description="kept", note="kept too", **kw)
    db_session.add(ip)
    await db_session.flush()
    db_session.add_all([
        IPHostnameObservation(ip_id=ip.id, source="netbios", hostname="OLD-MAC-NB", observed_at=then),
        IPHostnameObservation(ip_id=ip.id, source="opnsense", hostname="laptop-07", observed_at=then),
        IPHostnameReport(ip_id=ip.id, source="netbios", origin="netbios:agent-1", hostname="OLD-MAC-NB",
                         first_seen_at=then, last_seen_at=then),
        IPHostnameReport(ip_id=ip.id, source="mdns", origin="mdns:agent-1", hostname="old-mac.local",
                         first_seen_at=then, last_seen_at=then),
        IPHostnameReport(ip_id=ip.id, source="opnsense", origin="opnsense:fw-1", hostname="laptop-07",
                         first_seen_at=then, last_seen_at=then),
    ])
    await db_session.commit()
    return ip


async def _assert_previous_device_forgotten(db_session, ip):  # type: ignore[no-untyped-def]
    await db_session.refresh(ip)
    assert (ip.os_family, ip.os_guess, ip.device_kind, ip.device_model, ip.device_identified_at) == (
        None, None, None, None, None), "上一台的 OS／類型還掛著"
    assert (ip.os_ocs, ip.ocs_id, ip.ocs_tag, ip.ocs_agent, ip.ocs_notes, ip.ocs_hw, ip.last_seen_ocs) == (
        None, None, None, None, None, None, None), "上一台的 OCS 盤點資料還掛著"
    assert (ip.owner, ip.description, ip.note) == ("IT", "kept", "kept too"), "人填的不可以清"
    obs = {s for (s,) in (await db_session.execute(
        select(IPHostnameObservation.source).where(IPHostnameObservation.ip_id == ip.id))).all()}
    reports = {s for (s,) in (await db_session.execute(
        select(IPHostnameReport.source).where(IPHostnameReport.ip_id == ip.id))).all()}
    assert obs == {"opnsense"} and reports == {"opnsense"}, (obs, reports)
    logs = {(e, f, o) for e, f, o in (await db_session.execute(
        select(IPChangeLog.event_type, IPChangeLog.field, IPChangeLog.old_value)
        .where(IPChangeLog.ip_id == ip.id))).all()}
    assert ("os_changed", "os_family", "macos") in logs, "時間軸要看得出 OS 為什麼變成未知"
    assert ("kind_changed", "device_kind", "workstation") in logs


async def test_sync_mac_change_forgets_the_previous_device(db_session) -> None:
    ip = await _ip(db_session)
    assert await consider_mac(db_session, ip=ip, mac=NEW, source="opnsense") is True
    await db_session.commit()
    await _assert_previous_device_forgotten(db_session, ip)


async def test_first_mac_and_flap_back_keep_the_identity(db_session) -> None:
    """第一次填 MAC 不是換設備；24 小時內換回剛用過的 MAC＝同一批設備輪流出現（雙網卡 ARP flux），也不是。"""
    ip = await _ip(db_session)
    ip.mac = None
    await db_session.commit()
    await consider_mac(db_session, ip=ip, mac=OLD, source="opnsense")
    await db_session.commit()
    await db_session.refresh(ip)
    assert ip.os_family == "macos" and ip.ocs_id == 42

    await consider_mac(db_session, ip=ip, mac=NEW, source="opnsense")      # 真的換了 → 清
    await db_session.commit()
    ip.os_family, ip.device_kind = "linux", "server"                       # 新設備的辨識結果
    await db_session.commit()
    await consider_mac(db_session, ip=ip, mac=OLD, source="opnsense")      # 10 分鐘內又換回 → 輪流出現
    await db_session.commit()
    await db_session.refresh(ip)
    assert (ip.os_family, ip.device_kind) == ("linux", "server")


async def test_manual_mac_edit_forgets_the_previous_device(client, auth_headers, db_session) -> None:
    ip = await _ip(db_session)
    r = await client.patch(f"/api/v1/addresses/{ip.id}", headers=auth_headers, json={"mac": NEW})
    assert r.status_code == 200, r.text
    await _assert_previous_device_forgotten(db_session, ip)


async def test_mcp_mac_edit_forgets_the_previous_device(db_session, admin_user) -> None:
    from app.mcp import tools
    ip = await _ip(db_session)
    out = await tools.update_ip(db_session, user=admin_user, ip="198.51.100.162", mac=NEW)
    assert out.get("ok", True) is not False, out
    await db_session.commit()
    await _assert_previous_device_forgotten(db_session, ip)


async def test_saved_credentials_from_before_the_device_change_are_flagged(client, auth_headers, db_session) -> None:
    """綁在位址上的帳密，存下之後那個位址換了設備 → device_changed（前端不自動帶入，RDP 不驗憑證會直接送出去）。
    個人預設帳密（不綁位址）不受影響；換手之後才存（或改過）的也不標。"""
    ip = await _ip(db_session, rdp_enabled=True)
    base = {"auth_type": "password", "protocol": "rdp", "username": "admin", "password": "pw-1"}
    r1 = await client.post("/api/v1/ssh-credentials", headers=auth_headers,
                           json={**base, "label": "bound", "target_ip_id": str(ip.id)})
    r2 = await client.post("/api/v1/ssh-credentials", headers=auth_headers, json={**base, "label": "default"})
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    assert await consider_mac(db_session, ip=ip, mac=NEW, source="opnsense") is True     # 換手
    await db_session.commit()
    r3 = await client.post("/api/v1/ssh-credentials", headers=auth_headers,
                           json={**base, "label": "after", "target_ip_id": str(ip.id)})
    assert r3.status_code == 201, r3.text
    got = {c["label"]: c["device_changed"] for c in (await client.get(
        "/api/v1/ssh-credentials", headers=auth_headers,
        params={"target_ip_id": str(ip.id), "protocol": "rdp"})).json()}
    assert got == {"bound": True, "default": False, "after": False}, got


# ─────────────────── 其他綁在位址上的資料：拿 device_since 判斷新舊 ───────────────────

def test_rustdesk_peer_seen_only_before_the_device_change_is_not_mapped() -> None:
    from types import SimpleNamespace

    from app.services import rustdesk as rd
    now = datetime.now(UTC)
    addr = uuid.uuid4()
    peer = SimpleNamespace(registered_ip=None, report_ip="198.51.100.162", report_ip_at=now - timedelta(days=2),
                           online=False, last_online_at=now - timedelta(days=2), hostname=None)
    recs = {"198.51.100.162": [(addr, None, None, now - timedelta(days=1))]}
    status, mapped, _ev, _cand = rd._decide(peer, {}, recs, {}, {}, now)
    assert (status, mapped) == ("device_changed", None), "舊 peer 的心跳早於換手：IP 頁的連線按鈕會連到上一台"
    peer.report_ip_at = now - timedelta(hours=1)          # 換手之後才送來的心跳＝就是現在這台
    assert rd._decide(peer, {}, recs, {}, {}, now)[:2] == ("matched", addr)


def test_wazuh_agent_not_heard_since_the_device_change_does_not_represent_it() -> None:
    from types import SimpleNamespace

    from app.services.wazuh import agent_represents_ip
    now = datetime.now(UTC)
    ip = SimpleNamespace(device_since=now - timedelta(days=1), last_seen_scanner=None, last_seen_librenms=None,
                         arp_seen=None)
    assert agent_represents_ip(SimpleNamespace(status="active", last_keep_alive=now - timedelta(days=2)), ip) is False
    assert agent_represents_ip(SimpleNamespace(status="active", last_keep_alive=now), ip) is True


async def test_os_from_ocs_inventoried_before_the_device_change_is_ignored(db_session) -> None:
    from app.services.os_precedence import _candidates
    ip = await _ip(db_session)
    ip.device_since = datetime.now(UTC) - timedelta(days=1)       # last_seen_ocs 是 40 天前
    await db_session.commit()
    cand = await _candidates(db_session, ip)
    assert "ocs" not in cand


async def test_ai_dismissal_before_the_device_change_does_not_hide_the_new_device(db_session) -> None:
    from app.models.ai_finding import AIFinding
    from app.services.ai_audit import dismissed_subjects
    ip = await _ip(db_session)
    db_session.add(AIFinding(run_id=uuid.uuid4(), category="unauthorized", title="x", detail="",
                             evidence={"ips": ["198.51.100.162"]}, fingerprint="f" * 64, status="dismissed",
                             dismissed_at=datetime.now(UTC) - timedelta(days=3)))
    await db_session.commit()
    assert "198.51.100.162" in (await dismissed_subjects(db_session)).get("unauthorized", set())
    ip.device_since = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()
    assert "198.51.100.162" not in (await dismissed_subjects(db_session)).get("unauthorized", set()), \
        "忽略的是上一台；換手之後這個位址的同一件事要再列出來"


async def test_vm_matched_only_by_ip_with_a_different_mac_is_not_this_vm(db_session) -> None:
    from app.models.virt import VirtCluster, VirtualMachine, VMInterface
    from app.services.fw_lookup import vm_mac_conflict
    cl = VirtCluster(name=f"pve-{uuid.uuid4().hex[:6]}", type="proxmox")
    db_session.add(cl)
    await db_session.flush()
    vm = VirtualMachine(cluster_id=cl.id, name="vm-lab-02", kind="vm")
    db_session.add(vm)
    await db_session.flush()
    db_session.add(VMInterface(vm_id=vm.id, name="net0", mac="02:00:5e:00:53:02", primary_ip="198.51.100.162"))
    await db_session.commit()
    assert await vm_mac_conflict(db_session, vm.id, OLD) is True
    assert await vm_mac_conflict(db_session, vm.id, "02:00:5E:00:53:02") is False
    assert await vm_mac_conflict(db_session, vm.id, None) is False


async def test_probe_from_before_the_device_change_is_labelled(client, auth_headers, db_session) -> None:
    from app.models.agent_probe_job import STATUS_DONE, AgentProbeJob
    from app.models.scan_agent import ScanAgent
    ip = await _ip(db_session)
    ag = ScanAgent(name=f"ag-{uuid.uuid4().hex[:6]}")
    db_session.add(ag)
    await db_session.flush()
    then = datetime.now(UTC) - timedelta(days=5)
    db_session.add(AgentProbeJob(agent_id=ag.id, kind="identify", params={"targets": ["198.51.100.162"]},
                                 status=STATUS_DONE, result={"target": "198.51.100.162", "nmap": {"available": False}},
                                 created_at=then, finished_at=then, expires_at=then + timedelta(hours=1)))
    ip.device_since = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()
    r = await client.get(f"/api/v1/addresses/{ip.id}/identify", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["before_device_change"] is True


async def test_device_since_is_readable_on_the_ip(client, auth_headers, db_session) -> None:  # type: ignore[no-untyped-def]
    """API 讀得到換手時間（唯讀）；PATCH 帶它不會改到。"""
    import uuid as _uuid
    from datetime import UTC, datetime

    from app.models.address import IPAddress
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name=f"s-{_uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    when = datetime(2026, 8, 21, tzinfo=UTC)
    ipa = IPAddress(subnet_id=sub.id, ip="198.51.100.9", device_since=when)
    db_session.add(ipa)
    await db_session.commit()
    r = await client.get(f"/api/v1/addresses/{ipa.id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["device_since"].startswith("2026-08-21")
    r = await client.patch(f"/api/v1/addresses/{ipa.id}", headers=auth_headers,
                           json={"device_since": "2020-01-01T00:00:00Z"})
    assert r.status_code in (200, 422)
    again = await client.get(f"/api/v1/addresses/{ipa.id}", headers=auth_headers)
    assert again.json()["device_since"].startswith("2026-08-21")


async def _device_with_agent(db_session, *, device_since=None):  # type: ignore[no-untyped-def]
    """裝置＋它的 IP＋一個已離線的 Wazuh agent；IP 在 agent 離線之後還被掃到活著。"""
    import uuid as _uuid

    from app.models.address import IPAddress
    from app.models.device import Device
    from app.models.section import Section
    from app.models.subnet import Subnet
    from app.models.wazuh import WazuhAgent, WazuhInstance
    now = datetime.now(UTC)
    sec = Section(name=f"s-{_uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    dev = Device(name=f"srv-{_uuid.uuid4().hex[:6]}", type="server")
    db_session.add(dev)
    await db_session.flush()
    ipa = IPAddress(subnet_id=sub.id, ip="198.51.100.40", device_id=dev.id,
                    last_seen_scanner=now - timedelta(hours=1), device_since=device_since)
    db_session.add(ipa)
    inst = WazuhInstance(name=f"wz-{_uuid.uuid4().hex[:6]}", api_url="https://192.0.2.5:55000",
                         api_user="ro", api_password_enc=b"x", api_password_nonce=b"y")
    db_session.add(inst)
    await db_session.flush()
    db_session.add(WazuhAgent(instance_id=inst.id, agent_id="007", name="srv", ip="198.51.100.40",
                              status="disconnected", last_keep_alive=now - timedelta(days=3),
                              jt_ipam_address_id=ipa.id))
    await db_session.commit()
    return dev


async def test_device_page_still_shows_a_disconnected_agent(client, auth_headers, db_session) -> None:
    """裝置頁的 Wazuh 卡片顯示 agent 本身的狀態（含「離線」）。只有換手之前的 agent 才拿掉 ——
    「離線後位址又被看到活著」是 OS／弱點判讀用的規則，不能把卡片整張藏起來（發版 e2e 抓到的回歸）。"""
    dev = await _device_with_agent(db_session)
    r = await client.get(f"/api/v1/devices/{dev.id}/integrations", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["wazuh"] is not None
    assert r.json()["wazuh"]["status"] == "disconnected"


async def test_device_page_drops_an_agent_from_before_the_device_change(client, auth_headers, db_session) -> None:
    dev = await _device_with_agent(db_session, device_since=datetime.now(UTC) - timedelta(days=1))
    r = await client.get(f"/api/v1/devices/{dev.id}/integrations", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["wazuh"] is None
