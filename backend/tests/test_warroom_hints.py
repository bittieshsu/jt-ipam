"""戰情室的「未授權 IP」要附幾個判斷線索（使用者 10-22：「一眼看推測可能是啥主機或裝置」）。

線索只取現成資料、不另外探測：異常報告裡的 MAC 與廠牌、哪些來源看到；MAC 或 IP 對到虛擬化平台的 VM；
DNS 有名稱指向這個 IP。隨機（本地管理）MAC 要標出來（手機、筆電的隱私位址常見）。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.virt import VirtCluster, VirtualMachine, VMInterface
from app.services.system_config import set_anomaly_report
from app.services.warroom.hints import ip_hints

pytestmark = pytest.mark.anyio


async def test_hints_from_report_and_vm(db_session) -> None:
    await set_anomaly_report(db_session, {"unauthorized_ips": [
        {"ip": "198.51.100.31", "macs": [{"mac": "bc:24:11:00:00:31", "local": False, "vendor": "ProxmoxServe",
                                          "sources": ["librenms", "scanner"]}]},
        {"ip": "198.51.100.32", "macs": [{"mac": "a6:11:22:33:44:55", "local": True, "vendor": None, "sources": ["scanner"]}]},
    ]}, trigger="manual")
    cl = VirtCluster(name=f"cl-{uuid.uuid4().hex[:6]}", type="proxmox")
    db_session.add(cl)
    await db_session.flush()
    vm = VirtualMachine(cluster_id=cl.id, name="web-proxy", kind="qemu")
    db_session.add(vm)
    await db_session.flush()
    db_session.add(VMInterface(vm_id=vm.id, name="net0", mac="bc:24:11:00:00:31"))
    await db_session.commit()

    h = await ip_hints(db_session, ["198.51.100.31", "198.51.100.32", "198.51.100.99"])
    a = h["198.51.100.31"]
    assert a["mac"] == "bc:24:11:00:00:31" and a["vendor"] == "ProxmoxServe"
    assert a["vm"] == "web-proxy" and a["platform"] == "proxmox"
    assert a["sources"] == ["librenms", "scanner"]
    assert h["198.51.100.32"]["random_mac"] is True and "vendor" not in h["198.51.100.32"]
    assert "198.51.100.99" not in h                                        # 沒有任何線索就不出現


async def test_war_room_items_carry_hints(client, auth_headers, db_session) -> None:
    from datetime import UTC, datetime
    from app.models.anomaly_observation import AnomalyObservation
    await set_anomaly_report(db_session, {"unauthorized_ips": [
        {"ip": "198.51.100.41", "macs": [{"mac": "00:00:5e:00:53:41", "local": False, "vendor": "Synology",
                                          "sources": ["scanner"]}]}]}, trigger="manual")
    db_session.add(AnomalyObservation(category="unauthorized_ips", fingerprint="h" * 16, label="198.51.100.41",
                                      ip="198.51.100.41", first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC)))
    await db_session.commit()
    an = (await client.get("/api/v1/warroom/pages/anomalies", headers=auth_headers)).json()
    item = next(i for i in an["priority"]["items"] if i["label"] == "198.51.100.41")
    assert item["hints"]["vendor"] == "Synology"
    ov = (await client.get("/api/v1/warroom/pages/overview", headers=auth_headers)).json()
    p = next(i for i in ov["priority"]["items"] if i["value"] == "198.51.100.41")
    assert p["hints"]["mac"] == "00:00:5e:00:53:41"


async def test_hints_include_os_and_device_kind(db_session) -> None:
    """使用者 10-22：「如果有推測的 OS 或裝置類型也要顯示」。

    有 IP 記錄的：設備類型、型號、有效 OS（依 OS 來源優先序）；沒有 IP 記錄的（未授權 IP 常見）：
    對到的 VM 回報的 OS，或還代表這個 IP 的 Wazuh 代理。都沒有就不出現。
    """
    from app.models.address import IPAddress
    from app.models.section import Section
    from app.models.subnet import Subnet

    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db_session.add(sub)
    await db_session.flush()
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.51", state="used",
                             os_guess="Linux 5.X", os_family="linux", device_kind="printer", device_model="HP LaserJet"))
    cl = VirtCluster(name=f"cl-{uuid.uuid4().hex[:6]}", type="proxmox")
    db_session.add(cl)
    await db_session.flush()
    vm = VirtualMachine(cluster_id=cl.id, name="win-desk", kind="qemu", os_name="Windows 11 Pro")
    db_session.add(vm)
    await db_session.flush()
    db_session.add(VMInterface(vm_id=vm.id, name="net0", mac="bc:24:11:00:00:52", primary_ip="198.51.100.52"))
    await set_anomaly_report(db_session, {"unauthorized_ips": [
        {"ip": "198.51.100.52", "macs": [{"mac": "bc:24:11:00:00:52", "local": False, "vendor": "ProxmoxServe",
                                          "sources": ["scanner"]}]}]}, trigger="manual")
    await db_session.commit()

    h = await ip_hints(db_session, ["198.51.100.51", "198.51.100.52"])
    assert h["198.51.100.51"]["device_kind"] == "printer"
    assert h["198.51.100.51"]["device_model"] == "HP LaserJet"
    assert h["198.51.100.51"]["os"] == "Linux 5.X"
    assert h["198.51.100.52"]["os"] == "Windows 11 Pro"
    assert "device_kind" not in h["198.51.100.52"]


async def test_reference_categories_are_not_war_room_anomalies(client, auth_headers, db_session) -> None:
    """「只供參考、不通知」的 MAC 換埠（mac_drift_reference）不列進戰情室的時序、清單與數量（10-23）。"""
    from datetime import UTC, datetime
    from app.models.anomaly_observation import AnomalyObservation
    now = datetime.now(UTC)
    db_session.add(AnomalyObservation(category="mac_drift_reference", fingerprint="r" * 16, label="ref",
                                      first_seen_at=now, last_seen_at=now))
    db_session.add(AnomalyObservation(category="ghost_ips", fingerprint="g" * 16, label="198.51.100.77",
                                      ip="198.51.100.77", first_seen_at=now, last_seen_at=now))
    await db_session.commit()
    an = (await client.get("/api/v1/warroom/pages/anomalies", headers=auth_headers)).json()
    cats = {r["category"] for r in an["timeline"]["rows"]}
    assert "mac_drift_reference" not in cats
    assert "ghost_ips" in cats
    assert all(i["category"] != "mac_drift_reference" for i in an["priority"]["items"])
    assert an["metrics"]["open_anomalies"] == an["priority"]["total"]
    ov = (await client.get("/api/v1/warroom/pages/overview", headers=auth_headers)).json()
    assert all(i.get("category") != "mac_drift_reference" for i in ov["priority"]["items"])
