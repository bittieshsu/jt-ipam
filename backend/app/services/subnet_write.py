"""子網路的建立規則（REST 端點與 AI 工具共用）。

2026-10-15 MCP 工具盤點：AI 的 create_subnet 直接 `Subnet(cidr=…)` —— 不檢查重疊、不算上層子網路、
不繼承上層的單位。REST 有的規則 AI 沒有，於是 AI 建出來的網段在側邊選單掛錯地方、重疊網段也照建。
REST 的 POST 本體搬到這裡，兩邊呼叫同一份。

- 權限守門沿用 app/api/v1/write_guards（丟 HTTPException，與 REST 完全相同）；AI 工具那一側轉成工具錯誤
- 不寫稽核、不 commit；建立後的重算階層／索引說明／通知由呼叫端照自己的交易邊界做
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ui_error import detail_of, ui_detail
from app.models.section import Section
from app.models.subnet import Subnet
from app.schemas.subnet import SubnetCreate
from app.services.custom_field import CustomFieldError, validate_custom_fields
from app.services.permission import get_object_permission, has_permission
from app.services.subnet import SubnetOverlap, assert_no_overlap, compute_master_subnet


async def create_subnet_checked(session: AsyncSession, *, user: Any, payload: SubnetCreate) -> Subnet:
    """POST /subnets 的本體：區段寫入權、守門、重疊檢查、自訂欄位、上層子網路、繼承單位、主控台出口。"""
    from app.api.v1.write_guards import (
        require_admin_for_infra,
        require_admin_for_monitoring,
        require_parent_write,
    )
    # A01：要在指定 section 有 write 權限
    section = await session.get(Section, payload.section_id)
    if section is None:
        raise HTTPException(status_code=400, detail="Invalid section_id")
    level = await get_object_permission(
        session, user=user, object_type="section", object_id=section.id
    )
    if not has_permission(level, "write"):
        raise HTTPException(status_code=403, detail="Forbidden")
    requested = payload.model_dump(exclude_unset=True)
    require_admin_for_infra(user, requested)
    require_admin_for_monitoring(user, requested)
    await require_parent_write(session, user, "customer", payload.customer_id)

    try:
        await assert_no_overlap(
            session, cidr=payload.cidr, vrf_id=payload.vrf_id,
            allow_overlap=payload.allow_overlap,
        )
    except SubnetOverlap as exc:
        raise HTTPException(status_code=409, detail=detail_of(exc, "subnet_overlap")) from exc

    try:
        validated_cf = await validate_custom_fields(
            session, object_type="subnet", payload=payload.custom_fields
        )
    except CustomFieldError as exc:
        raise HTTPException(status_code=400, detail=detail_of(exc, "custom_field_error")) from exc

    master_id = await compute_master_subnet(
        session, cidr=payload.cidr, vrf_id=payload.vrf_id
    )

    data = payload.model_dump()
    data.pop("allow_overlap", None)   # 僅建立時的旗標，非欄位
    data["master_subnet_id"] = master_id
    # 新增的若是某子網路的下層、且未指定單位 → 繼承父網段的單位
    #（側邊選單依單位分組，這樣子網段才會自動歸到與父網段同一個單位群組下）
    if not data.get("customer_id") and master_id:
        parent = await session.get(Subnet, master_id)
        if parent is not None and parent.customer_id is not None:
            data["customer_id"] = parent.customer_id
    data["custom_fields"] = validated_cf or None

    # 主控台出口（跳板／掃描代理擇一）的驗證；子網路與 IP 的編輯共用 console_route.normalize_egress
    from app.services.console_route import EgressError, normalize_egress
    try:
        await normalize_egress(session, data, scan_agent_id=data.get("scan_agent_id"))
    except EgressError as exc:
        raise HTTPException(status_code=422, detail=ui_detail(exc.code, str(exc), **exc.params)) from exc
    subnet = Subnet(**data)
    session.add(subnet)
    await session.flush()
    return subnet
