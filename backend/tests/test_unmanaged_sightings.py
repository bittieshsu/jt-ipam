"""沒有納管、但看得到在用的位址（使用者 2026-10-06：「就算關閉自動加，也要讓格子看得出來該 IP 被用、但不是我們納管」）。

自動收錄關閉時，掃描代理掃到 IPAM 裡沒有的活位址以前直接丟掉，指示計上跟閒置一模一樣，
說明文字還寫「仍會出現在異常偵測裡」（其實不會）。現在只記「看到過」，不建 IP 記錄。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.address import IPAddress
from app.models.scan_agent import ScanAgent
from app.models.unmanaged_sighting import UnmanagedSighting
from sqlalchemy import func, select

from tests.test_scan_agent_auto_create import _fixture


async def _report(client, monkeypatch, agent_id, results):
    from app.api.v1.endpoints import scan_agents as ep

    async def _fake_agent(session, key):
        return (await session.execute(select(ScanAgent).where(ScanAgent.id == agent_id))).scalar_one()

    monkeypatch.setattr(ep, "_agent_from_key", _fake_agent)
    r = await client.post("/api/v1/scan-agents/report", headers={"X-Agent-Key": "x"}, json={"results": results})
    assert r.status_code == 200, r.text
    return r.json()


async def test_unknown_live_address_is_remembered_not_created(db_session, client, monkeypatch, auth_headers) -> None:
    sub, agent = await _fixture(db_session, auto=False)
    sid, aid = sub.id, agent.id
    await db_session.commit()
    body = await _report(client, monkeypatch, aid, [
        {"ip": "198.51.100.77", "alive": True, "mac": "00:00:5e:00:53:77", "netbios": "LAPTOP-07"},
        {"ip": "203.0.113.9", "alive": True},           # 不在任何指派給代理的子網路 → 不記
        {"ip": "198.51.100.78", "alive": True, "liveness": False},   # 背景探測補的資料不是上線證據
    ])
    assert body["skipped_not_in_ipam"] == 2 and body.get("created", 0) == 0   # 兩個都沒收錄（只有一個在範圍內）
    db_session.expire_all()
    assert (await db_session.scalar(select(func.count()).select_from(IPAddress)
                                    .where(IPAddress.subnet_id == sid))) == 0
    rows = (await db_session.execute(select(UnmanagedSighting).where(UnmanagedSighting.subnet_id == sid))).scalars().all()
    assert [str(r.ip).split("/")[0] for r in rows] == ["198.51.100.77"]
    assert rows[0].mac == "00:00:5e:00:53:77" and rows[0].hostname == "LAPTOP-07"

    r = await client.get("/api/v1/addresses/unmanaged", headers=auth_headers, params={"subnet_id": str(sid)})
    assert r.status_code == 200, r.text
    (u,) = r.json()
    assert u["ip"] == "198.51.100.77" and u["sources"] == ["scanner"] and u["last_seen_at"]


async def test_second_report_updates_the_same_row_and_keeps_first_seen(db_session, client, monkeypatch) -> None:
    sub, agent = await _fixture(db_session, auto=False)
    sid, aid = sub.id, agent.id
    await db_session.commit()
    await _report(client, monkeypatch, aid, [{"ip": "198.51.100.80", "alive": True, "mac": "00:00:5e:00:53:80"}])
    db_session.expire_all()
    first = (await db_session.execute(select(UnmanagedSighting))).scalars().one()
    f0 = first.first_seen_at
    await _report(client, monkeypatch, aid, [{"ip": "198.51.100.80", "alive": True}])   # 這次沒有 MAC
    db_session.expire_all()
    again = (await db_session.execute(select(UnmanagedSighting))).scalars().one()
    assert again.first_seen_at == f0 and again.last_seen_at >= f0
    assert again.mac == "00:00:5e:00:53:80", "這次沒回報 MAC 不可以把上次的清掉"


async def test_registered_addresses_are_not_listed(db_session, client, monkeypatch, auth_headers) -> None:
    sub, agent = await _fixture(db_session, auto=False)
    sid, aid = sub.id, agent.id
    await db_session.commit()
    await _report(client, monkeypatch, aid, [{"ip": "198.51.100.81", "alive": True}])
    db_session.add(IPAddress(subnet_id=sid, ip="198.51.100.81"))
    await db_session.commit()
    r = await client.get("/api/v1/addresses/unmanaged", headers=auth_headers, params={"subnet_id": str(sid)})
    assert r.json() == []


async def test_old_sightings_are_purged(db_session) -> None:
    from app.services.unmanaged import purge
    sub, agent = await _fixture(db_session, auto=False)
    old = datetime.now(UTC) - timedelta(days=31)
    db_session.add_all([
        UnmanagedSighting(subnet_id=sub.id, ip="198.51.100.90", source="scanner", first_seen_at=old, last_seen_at=old),
        UnmanagedSighting(subnet_id=sub.id, ip="198.51.100.91", source="scanner",
                          first_seen_at=old, last_seen_at=datetime.now(UTC)),
    ])
    await db_session.flush()
    assert await purge(db_session) == 1
    left = [str(r.ip).split("/")[0] for r in (await db_session.execute(select(UnmanagedSighting))).scalars()]
    assert left == ["198.51.100.91"]


async def test_unauthorized_ip_detection_sees_scanner_sightings(db_session) -> None:
    """以前「未授權 IP」只讀 LibreNMS 的 ARP 表，掃描代理看到的未登錄位址完全不會出現。"""
    from app.services.anomaly import detect_unauthorized_ips
    sub, agent = await _fixture(db_session, auto=False)
    sub.anomaly_enabled = True
    now = datetime.now(UTC)
    db_session.add(UnmanagedSighting(subnet_id=sub.id, ip="198.51.100.95", source="scanner",
                                     mac="00:00:5e:00:53:95", first_seen_at=now, last_seen_at=now))
    await db_session.flush()
    rows = await detect_unauthorized_ips(db_session)
    hit = [r for r in rows if r["ip"] == "198.51.100.95"]
    assert hit, rows
    assert hit[0]["macs"][0]["sources"] == ["scanner"]


async def test_unmanaged_list_needs_subnet_read_permission(db_session, client) -> None:
    from tests.test_rbac_enforcement import _nonadmin_token
    sub, _agent = await _fixture(db_session, auto=False)
    sid = sub.id
    _u, token = await _nonadmin_token(db_session)
    await db_session.commit()
    r = await client.get("/api/v1/addresses/unmanaged", headers={"Authorization": f"Bearer {token}"},
                         params={"subnet_id": str(sid)})
    assert r.status_code == 404
    assert isinstance(sid, uuid.UUID)
