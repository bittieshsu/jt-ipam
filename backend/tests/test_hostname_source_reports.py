"""主機名稱來源要看得出「哪一台回報的、最後一次是什麼時候」（客戶 2026-10-11）。

客戶在比對各台掃描代理抓到的名字（NetBIOS 名稱和 DNS 主機名稱在 Windows 上可以不一樣，被坑過），
想知道每個名字到底是哪裡來的。以前面板只寫來源類型（「NetBIOS（掃描代理）」），而且顯示的時間是
observations.observed_at —— 那只在名稱改變時才更新，已經沒人回報的殘留名稱看起來跟剛回報的一樣。
逐台記錄（ip_hostname_reports，含 last_seen_at）本來就有，這裡把它帶到 API。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.address import IPAddress
from app.models.ip_hostname import IPHostnameReport
from app.models.scan_agent import ScanAgent
from app.models.section import Section
from app.models.subnet import Subnet
from app.models.wazuh import WazuhInstance
from app.services.hostname import apply_observation
from app.services.hostname_reports import HostnameRun
from sqlalchemy import update


async def _mk_ip(session) -> IPAddress:
    sec = Section(name=f"hsr-{uuid.uuid4().hex[:6]}")
    session.add(sec)
    await session.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    session.add(sub)
    await session.flush()
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.20")
    session.add(ip)
    await session.flush()
    return ip


async def test_reports_name_the_reporting_agent_and_last_seen(client, db_session, auth_headers):
    ip = await _mk_ip(db_session)
    a1 = ScanAgent(name="agent-east", enroll_key_hash="x" * 64, enabled=True)
    a2 = ScanAgent(name="agent-west", enroll_key_hash="y" * 64, enabled=True)
    wz = WazuhInstance(name="wazuh-main", api_url="https://192.0.2.5:55000", api_user="u",
                       api_password_enc=b"x", api_password_nonce=b"y")
    db_session.add_all([a1, a2, wz])
    await db_session.flush()
    for agent, name in ((a1, "FILESRV01"), (a2, "FILESRV01")):
        run = HostnameRun(db_session, source="netbios", origin=f"netbios:{agent.id}", peers=2)
        run.report(ip, name)
        await run.finish(complete=False)
    run = HostnameRun(db_session, source="wazuh", origin=f"wazuh:{wz.id}")
    run.report(ip, "fileserver-2019-prod")
    await run.finish(complete=False)
    # agent-west 是三天前回報的（之後關掉了 NetBIOS 探測）
    old = datetime.now(UTC) - timedelta(days=3)
    await db_session.execute(update(IPHostnameReport).where(
        IPHostnameReport.origin == f"netbios:{a2.id}").values(last_seen_at=old, first_seen_at=old))
    await db_session.commit()

    body = (await client.get(f"/api/v1/addresses/{ip.id}/hostname-sources", headers=auth_headers)).json()
    obs = {o["source"]: o for o in body["observations"]}
    nb = obs["netbios"]["reports"]
    assert {r["label"] for r in nb} == {"agent-east", "agent-west"}
    assert all(r["kind"] == "scan_agent" for r in nb)
    west = next(r for r in nb if r["label"] == "agent-west")
    east = next(r for r in nb if r["label"] == "agent-east")
    assert west["last_seen_at"] < east["last_seen_at"]
    assert west["hostname"] == "FILESRV01"
    # 觀測的 last_seen_at＝各台裡最新的一次（面板要顯示的是「最後一次有人回報」，不是名稱最後改變）
    assert obs["netbios"]["last_seen_at"] == east["last_seen_at"]
    assert obs["wazuh"]["reports"][0]["label"] == "wazuh-main"


async def test_manual_and_legacy_have_no_reporter(client, db_session, auth_headers):
    ip = await _mk_ip(db_session)
    await apply_observation(db_session, ip=ip, source="manual", hostname="typed-by-hand")
    db_session.add(IPHostnameReport(ip_id=ip.id, source="dns", origin="legacy", hostname="old-name"))
    db_session.add(IPHostnameReport(ip_id=ip.id, source="dns", origin=f"dns:{uuid.uuid4()}", hostname="gone-server"))
    await apply_observation(db_session, ip=ip, source="dns", hostname="old-name")
    await db_session.commit()

    body = (await client.get(f"/api/v1/addresses/{ip.id}/hostname-sources", headers=auth_headers)).json()
    obs = {o["source"]: o for o in body["observations"]}
    assert obs["manual"]["reports"] == []
    kinds = {r["kind"] for r in obs["dns"]["reports"]}
    # legacy＝遷移前的舊資料；找不到那台（已刪除）的不可以拿 id 亂猜名字
    assert kinds == {"legacy", "unknown"}
    assert all(r["label"] is None for r in obs["dns"]["reports"])


def test_every_reporting_source_maps_to_its_instance_table() -> None:
    """新整合回報主機名稱時要加進 ORIGIN_MODELS，否則面板只能說「不明」。"""
    from app.models.ip_hostname import HOSTNAME_SOURCES
    from app.services.hostname_reports import NO_ORIGIN_SOURCES, ORIGIN_MODELS, _model
    missing = [s for s in HOSTNAME_SOURCES if s not in ORIGIN_MODELS and s not in NO_ORIGIN_SOURCES]
    assert not missing, f"這些主機名稱來源沒有對應的實例表：{missing}"
    for src in ORIGIN_MODELS:
        m = _model(src)
        assert hasattr(m, "id"), src
        assert hasattr(m, "name"), src
