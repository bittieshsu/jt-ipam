"""同一台掃描代理同時送兩筆回報：要排隊處理，不可以互相死結。

prod 實況（2026-10-08 盤點）：兩週內 28 次 `POST /scan-agents/report` 回 500，全部是
`DeadlockDetectedError`。代理會同時送「逐子網路立刻回報」與背景探測結果（逾時重送也會），
兩筆交易涵蓋同一批 IP、卻在不同階段取得列鎖（ORM flush 先鎖一批、重算主機名稱時再鎖另一批），
交錯時就互鎖。同一台代理的回報在交易內以 advisory lock 排隊；不同代理照常平行。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from app.models.address import IPAddress
from app.models.scan_agent import ScanAgent
from app.models.section import Section
from app.models.subnet import Subnet


async def _fixture(db, n: int = 40):  # type: ignore[no-untyped-def]
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24", scan_enabled=True)
    agent = ScanAgent(name=f"a-{uuid.uuid4().hex[:6]}")
    db.add_all([sub, agent])
    await db.flush()
    sub.scan_agent_id = agent.id
    for i in range(n):
        db.add(IPAddress(subnet_id=sub.id, ip=f"198.51.100.{10 + i}", state="active"))
    await db.commit()
    return sub, agent


def _patch_agent(monkeypatch, agent_id) -> None:  # type: ignore[no-untyped-def]
    from app.api.v1.endpoints import scan_agents as ep

    async def _fake_agent(session, key):  # noqa: ANN001
        return (await session.execute(select(ScanAgent).where(ScanAgent.id == agent_id))).scalar_one()

    monkeypatch.setattr(ep, "_agent_from_key", _fake_agent)


def _report(ips: list[str], tag: str) -> dict:
    return {"results": [{"ip": ip, "alive": True, "rdns": f"{tag}-{ip.rsplit('.', 1)[1]}.example.net",
                         "probes_run": ["rdns"]} for ip in ips]}


@pytest.mark.anyio
async def test_reports_from_one_agent_wait_for_each_other(db_session, client, _engine, monkeypatch) -> None:
    """另一筆回報還在交易裡時，同一台代理的下一筆要等它結束才動手。"""
    from app.api.v1.endpoints.scan_agents import report_lock_key
    _sub, agent = await _fixture(db_session, n=3)
    _patch_agent(monkeypatch, agent.id)
    async with _engine.connect() as held:
        await held.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": report_lock_key(agent.id)})
        task = asyncio.create_task(client.post("/api/v1/scan-agents/report", headers={"X-Agent-Key": "x"},
                                               json=_report(["198.51.100.10"], "a")))
        await asyncio.sleep(1.0)
        assert not task.done(), "同一台代理的回報沒有排隊：兩筆交易會同時鎖同一批 IP"
        await held.rollback()
    r = await asyncio.wait_for(task, 20)
    assert r.status_code == 200, r.text


@pytest.mark.anyio
async def test_two_overlapping_reports_at_once_both_succeed(db_session, client, monkeypatch) -> None:
    """同一批 IP、相反順序、同時送：兩筆都要成功，名稱以後到的為準（不是 500）。"""
    _sub, agent = await _fixture(db_session)
    _patch_agent(monkeypatch, agent.id)
    ips = [f"198.51.100.{10 + i}" for i in range(40)]
    r1, r2 = await asyncio.gather(
        client.post("/api/v1/scan-agents/report", headers={"X-Agent-Key": "x"}, json=_report(ips, "a")),
        client.post("/api/v1/scan-agents/report", headers={"X-Agent-Key": "x"}, json=_report(ips[::-1], "b")))
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
