"""「未裝 Agent 的 IP」要能依子網路／區段／單位篩選（Wazuh 與 OCS 兩頁都要）。

端點替每一筆補上所屬的子網路、區段與單位，前端據此篩選。
單位的判斷跟權限一致（授權上層就涵蓋下層）：IP 自己掛的 → 子網路的 → 區段的。
"""
from __future__ import annotations

import pytest


async def _setup(session):
    from app.models.address import IPAddress
    from app.models.customer import Customer
    from app.models.section import Section
    from app.models.subnet import Subnet

    acme = Customer(name="ACME")
    beta = Customer(name="Beta")
    session.add_all([acme, beta])
    await session.flush()
    sec = Section(name="Branch", customer_id=acme.id)       # 單位掛在區段
    session.add(sec)
    await session.flush()
    net = Subnet(section_id=sec.id, cidr="198.51.100.0/24", description="lab")
    session.add(net)
    await session.flush()
    inherit = IPAddress(subnet_id=net.id, ip="198.51.100.30", hostname="inherits-acme")
    own = IPAddress(subnet_id=net.id, ip="198.51.100.31", hostname="own-beta", customer_id=beta.id)
    session.add_all([inherit, own])
    await session.flush()
    await session.commit()
    return {"acme": acme, "beta": beta, "sec": sec, "net": net}


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/api/v1/ocs/missing-agents", "/api/v1/wazuh/missing-agents"])
async def test_missing_rows_carry_subnet_section_and_customer(client, auth_headers, db_session, path) -> None:
    ctx = await _setup(db_session)
    r = await client.get(path, headers=auth_headers)
    assert r.status_code == 200, r.text
    rows = {x["ip"]: x for x in r.json()}
    a = rows["198.51.100.30"]
    assert a["subnet_id"] == str(ctx["net"].id) and a["subnet_cidr"] == "198.51.100.0/24"
    assert a["section_id"] == str(ctx["sec"].id) and a["section_name"] == "Branch"
    assert (a["customer_id"], a["customer_name"]) == (str(ctx["acme"].id), "ACME"), \
        "IP 與子網路都沒掛單位時，要沿用區段的單位"
    b = rows["198.51.100.31"]
    assert (b["customer_id"], b["customer_name"]) == (str(ctx["beta"].id), "Beta"), \
        "IP 自己掛的單位優先"
