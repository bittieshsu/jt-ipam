"""替「未裝 Agent 的 IP」補上所屬的子網路、區段與單位（Wazuh 與 OCS 整合頁共用）。

畫面要能依這三種篩選。單位的判斷跟權限一致（授權上層就涵蓋下層）：
IP 自己掛的單位 → 子網路的 → 區段的。只看 IP 自己的欄位的話，單位掛在區段上的
整批 IP 都會篩不出來。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.address import IPAddress
from app.models.customer import Customer
from app.models.section import Section
from app.models.subnet import Subnet


async def annotate_scope(session: AsyncSession, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [uuid.UUID(str(r["ip_address_id"])) for r in rows if r.get("ip_address_id")]
    if not ids:
        return rows
    info = {}
    for rid, ip_cust, sub_id, cidr, sub_cust, sec_id, sec_name, sec_cust in (await session.execute(
        select(IPAddress.id, IPAddress.customer_id, Subnet.id, Subnet.cidr, Subnet.customer_id,
               Section.id, Section.name, Section.customer_id)
        .join(Subnet, Subnet.id == IPAddress.subnet_id)
        .join(Section, Section.id == Subnet.section_id, isouter=True)
        .where(IPAddress.id.in_(ids))
    )).all():
        info[str(rid)] = (ip_cust or sub_cust or sec_cust, sub_id, cidr, sec_id, sec_name)
    cust_ids = {v[0] for v in info.values() if v[0]}
    names = dict((await session.execute(
        select(Customer.id, Customer.name).where(Customer.id.in_(cust_ids))
    )).all()) if cust_ids else {}
    for r in rows:
        cust, sub_id, cidr, sec_id, sec_name = info.get(str(r.get("ip_address_id")), (None,) * 5)
        r.update({
            "subnet_id": str(sub_id) if sub_id else None,
            "subnet_cidr": str(cidr) if cidr else None,
            "section_id": str(sec_id) if sec_id else None,
            "section_name": sec_name,
            "customer_id": str(cust) if cust else None,
            "customer_name": names.get(cust) if cust else None,
        })
    return rows
