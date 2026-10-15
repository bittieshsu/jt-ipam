"""IP 狀態「準備退場」「隔離中」要存得進去（2026-10-15 盤點 AI 工具時發現）。

畫面的狀態選單與 API schema 有 deprecated／quarantine，資料表 CHECK 約束卻沒有：選了就是 500。0218 放寬約束。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.address import IPAddress
from app.models.section import Section
from app.models.subnet import Subnet

pytestmark = pytest.mark.anyio


async def _ip(db) -> IPAddress:  # type: ignore[no-untyped-def]
    sec = Section(name=f"st-{uuid.uuid4().hex[:6]}")
    db.add(sec)
    await db.flush()
    sub = Subnet(section_id=sec.id, cidr="198.51.100.0/24")
    db.add(sub)
    await db.flush()
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.9", state="active")
    db.add(ip)
    await db.commit()
    return ip


@pytest.mark.parametrize("state", ["deprecated", "quarantine"])
async def test_lifecycle_states_can_be_saved(client, db_session, auth_headers, state) -> None:
    ip = await _ip(db_session)
    r = await client.patch(f"/api/v1/addresses/{ip.id}", json={"state": state}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == state


async def test_schema_and_table_constraint_agree() -> None:
    """schema 允許的狀態都要寫得進資料表（AI 工具也靠這個交集決定能提供哪些狀態）。"""
    from app.mcp.tools import _writable_ip_states
    from app.schemas.address import IP_STATES
    assert _writable_ip_states() == IP_STATES
