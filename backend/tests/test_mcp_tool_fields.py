"""AI 工具要回傳畫面上看得到的欄位（使用者 2026-10-15）。

起因：問「電路的申請日與到期日要去哪裡查」，AI 回答「電路沒有這兩個欄位」—— 編輯畫面明明有「開通日」
「合約到期」，是 `list_circuits` 沒回傳 `install_date`／`contract_end_date`，模型只看得到工具給的欄位，
就斷言系統沒有。工具少給一個欄位，AI 就會很有把握地說錯。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.mcp.tools import TOOLS

pytestmark = pytest.mark.anyio


async def test_circuits_return_dates_dns_and_tenant(db_session, admin_user) -> None:
    from app.models.advanced import Circuit, Provider, Tenant
    p = Provider(name=f"isp-{uuid.uuid4().hex[:6]}")
    tn = Tenant(name=f"t-{uuid.uuid4().hex[:6]}", slug=f"t-{uuid.uuid4().hex[:6]}")
    db_session.add_all([p, tn])
    await db_session.flush()
    end = (datetime.now(UTC) + timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    db_session.add(Circuit(cid="HN-1", provider_id=p.id, tenant_id=tn.id, dns_servers="192.0.2.53",
                           install_date=datetime(2024, 3, 1, tzinfo=UTC), contract_end_date=end))
    await db_session.flush()
    out = await TOOLS["list_circuits"]["fn"](db_session, user=admin_user)
    row = next(c for c in out["circuits"] if c["cid"] == "HN-1")
    assert row["install_date"] == "2024-03-01"
    assert row["contract_end_date"] == end.date().isoformat()
    assert row["contract_days_left"] in (29, 30)
    assert row["dns_servers"] == "192.0.2.53"
    assert row["tenant"] == tn.name
    desc = TOOLS["list_circuits"]["description"]
    assert "install_date" in desc and "contract_end_date" in desc, "說明要讓模型知道有日期欄位"
