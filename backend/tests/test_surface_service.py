"""對外開放的埠上跑的是什麼：證據等級（使用者 2026-10-19 定案，第一版只用現有資料）。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services import surface_service as ss

pytestmark = pytest.mark.anyio


def _item(port: Any, proto: str | None = "tcp", name: str = "", hostname: str | None = None,
          fqdns: list[str] | None = None) -> dict[str, Any]:
    return {"via": "nat", "source": "opnsense", "firewall": "fw", "name": name, "protocol": proto, "port": port,
            "descr": "", "identity": {"registered": True, "ip": "198.51.100.5", "hostname": hostname,
                                      "fqdns": fqdns or []}}


def test_port_alone_is_only_a_guess() -> None:
    s = ss.identify(_item(22, "tcp", name="rule-22"), None)
    assert (s["name"], s["level"]) == ("SSH", "port_guess")
    assert s["sensitive"] is False
    assert s["unverified_sensitive"] is True


def test_names_beat_the_port_number() -> None:
    """使用者的例子：UDP 10000 是 Jitsi Videobridge，規則名稱說了算。"""
    s = ss.identify(_item(10000, "udp", name="Jitsi Meet - Step - Videobridge", hostname="meet"), None)
    assert (s["name"], s["level"]) == ("Jitsi Meet", "named")
    assert s["sensitive"] is False
    assert s["unverified_sensitive"] is False
    assert "規則名稱" in s["evidence"][0]


def test_probe_confirms_and_flags_a_mismatch() -> None:
    probe = [{"port": 4443, "proto": "tcp", "service": "ssh", "product": "OpenSSH", "version": "9.6",
              "method": "probed"}]
    s = ss.identify(_item(4443, "tcp", name="Jitsi Meet - Web 2"), probe)
    assert (s["name"], s["level"]) == ("OpenSSH 9.6", "confirmed")
    assert s["sensitive"] is True
    assert s["mismatch"] == "名稱看起來是 Jitsi Meet，實際探測到 OpenSSH 9.6"


def test_nmap_table_guesses_are_not_confirmations() -> None:
    probe = [{"port": 10000, "proto": "tcp", "service": "snet-sensor-mgmt", "method": "table"}]
    s = ss.identify(_item(10000, "tcp", name="something"), probe)
    assert s["level"] == "port_guess"


def test_confirmed_web_with_title() -> None:
    probe = [{"port": 443, "proto": "tcp", "service": "https", "product": "nginx", "method": "probed",
              "scripts": {"http-title": "Jitsi Meet"}}]
    s = ss.identify(_item(443, "tcp"), probe)
    assert s["level"] == "confirmed"
    assert s["sensitive"] is False
    assert "Jitsi Meet" in s["evidence"][0]


async def test_probe_results_after_the_address_changed_hands_only(db_session) -> None:
    from app.models.address import IPAddress
    from app.models.agent_probe_job import STATUS_DONE, AgentProbeJob
    from app.models.scan_agent import ScanAgent
    from app.models.section import Section
    from app.models.subnet import Subnet
    sec = Section(name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    agent = ScanAgent(name=f"a-{uuid.uuid4().hex[:6]}")
    db_session.add_all([sub, agent])
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add(IPAddress(subnet_id=sub.id, ip="198.51.100.5", device_since=now - timedelta(days=1)))
    for days, product in ((3, "old box"), (0, "OpenSSH")):
        db_session.add(AgentProbeJob(agent_id=agent.id, kind="identify", params={"targets": ["198.51.100.5"]},
                                     status=STATUS_DONE, created_at=now - timedelta(days=days),
                                     expires_at=now + timedelta(days=1),
                                     result={"nmap": {"ports": [{"port": 22, "proto": "tcp", "service": "ssh",
                                                                 "product": product, "method": "probed"}]}}))
    await db_session.commit()
    got = await ss.probe_ports_by_ip(db_session, {"198.51.100.5", "198.51.100.6"})
    assert got["198.51.100.5"][0]["product"] == "OpenSSH"
    assert "198.51.100.6" not in got
