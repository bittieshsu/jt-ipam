"""掃描代理只能更新「指派給它的子網路」裡的位址。

以前回報端點寫的是 `if agent_subnet_ids: 只在這些子網路比對` —— 沒有指派任何子網路的代理
因此**跳過整道過濾**，可以改全站任何一筆既有 IP 的 MAC、OS、主機名稱與上線時間。
一把剛建立、還沒設定的代理金鑰外流，就等於能改寫整個 IPAM 的觀測資料。

代理端點另外加上逐代理限流：金鑰正確但行為異常（迴圈、被濫用）時不至於拖垮伺服器。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.address import IPAddress
from app.models.scan_agent import ScanAgent
from app.models.section import Section
from app.models.subnet import Subnet


async def _existing_ip(db_session, cidr: str, ip: str):
    sec = Section(name=f"sec-{uuid.uuid4().hex[:6]}")
    db_session.add(sec)
    await db_session.flush()
    sub = Subnet(section_id=sec.id, cidr=cidr, scan_enabled=True)
    db_session.add(sub)
    await db_session.flush()
    ipa = IPAddress(subnet_id=sub.id, ip=ip, state="active")
    db_session.add(ipa)
    await db_session.flush()
    return sub, ipa


def _as_agent(monkeypatch, agent_id):
    from app.api.v1.endpoints import scan_agents as ep

    async def _fake_agent(session, key):   # noqa: ANN001
        return (await session.execute(
            select(ScanAgent).where(ScanAgent.id == agent_id))).scalar_one()

    monkeypatch.setattr(ep, "_agent_from_key", _fake_agent)


async def _reload(db_session, ipa_id):
    db_session.expire_all()
    return (await db_session.execute(select(IPAddress).where(IPAddress.id == ipa_id))).scalar_one()


@pytest.mark.anyio
async def test_agent_without_subnets_cannot_touch_any_ip(db_session, client, monkeypatch) -> None:
    _sub, ipa = await _existing_ip(db_session, "198.51.100.0/24", "198.51.100.20")
    agent = ScanAgent(name=f"a-{uuid.uuid4().hex[:6]}", auto_create_ips=True)
    db_session.add(agent)
    await db_session.commit()
    _as_agent(monkeypatch, agent.id)

    r = await client.post("/api/v1/scan-agents/report", headers={"X-Agent-Key": "x"}, json={"results": [
        {"ip": "198.51.100.20", "alive": True, "mac": "02:00:5e:10:00:01", "rdns": "evil.example"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 0 and body["created"] == 0
    assert body["skipped_no_subnet"] == 1, "沒有指派子網路要講出來，不是靜靜丟掉"
    got = await _reload(db_session, ipa.id)
    assert got.mac is None, "沒有指派子網路的代理改到了別人的 IP"
    assert got.last_seen_scanner is None


@pytest.mark.anyio
async def test_agent_cannot_touch_ips_outside_its_subnets(db_session, client, monkeypatch) -> None:
    mine, _ = await _existing_ip(db_session, "192.0.2.0/24", "192.0.2.5")
    _other, theirs = await _existing_ip(db_session, "203.0.113.0/24", "203.0.113.9")
    agent = ScanAgent(name=f"a-{uuid.uuid4().hex[:6]}")
    db_session.add(agent)
    await db_session.flush()
    mine.scan_agent_id = agent.id
    await db_session.commit()
    _as_agent(monkeypatch, agent.id)

    r = await client.post("/api/v1/scan-agents/report", headers={"X-Agent-Key": "x"}, json={"results": [
        {"ip": "192.0.2.5", "alive": True, "mac": "02:00:5e:10:00:02"},
        {"ip": "203.0.113.9", "alive": True, "mac": "02:00:5e:10:00:03"}]})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1
    got = await _reload(db_session, theirs.id)
    assert got.mac is None, "代理改到了沒有指派給它的子網路"


@pytest.mark.anyio
async def test_agent_endpoints_are_rate_limited_per_agent(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.core import rate_limit
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "rate_limit_agent", "3/minute")
    agent_id = uuid.uuid4()
    for _ in range(3):
        await rate_limit.limit_agent("scan", agent_id)
    with pytest.raises(HTTPException) as ei:
        await rate_limit.limit_agent("scan", agent_id)
    assert ei.value.status_code == 429
    # 另一台代理不受影響（以代理為單位，不是來源位址：同一個 NAT 後面可能有很多台）
    await rate_limit.limit_agent("scan", uuid.uuid4())


@pytest.mark.anyio
async def test_agent_rate_limit_fails_open_when_redis_is_down(monkeypatch) -> None:
    """代理回報是監控資料的來源；限流本身壞掉不該讓所有代理停擺。"""
    from app.core import rate_limit
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)

    async def _boom(**_kw):   # noqa: ANN003
        raise ConnectionError("redis down")

    monkeypatch.setattr(rate_limit, "check_rate_limit", _boom)
    await rate_limit.limit_agent("scan", uuid.uuid4())


def test_every_agent_key_lookup_applies_the_limit() -> None:
    """三種代理（掃描、憑證、RustDesk）用金鑰找代理的函式都要套限流。"""
    import inspect

    from app.api.v1.endpoints import cert_agents, rustdesk_agent, scan_agents

    for fn in (scan_agents._agent_from_key, cert_agents._agent_from_key, rustdesk_agent._server_from_key):
        assert "limit_agent(" in inspect.getsource(fn), f"{fn.__module__}.{fn.__name__} 沒有限流"
