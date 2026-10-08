"""Technitium DHCP endpoints（admin only）：設定、測試連線、手動拉取（背景作業）、範圍鏡像。

DNS 那一半在 /dns/servers（type = technitium），這裡只管 DHCP。寫法比照 Kea（endpoints/dhcp_standalone.py）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.ui_error import detail_of
from app.models.technitium import TechnitiumDhcpScope, TechnitiumDhcpServer
from app.schemas.base import Paginated
from app.schemas.technitium import (
    TechnitiumDhcpCreate,
    TechnitiumDhcpRead,
    TechnitiumDhcpUpdate,
    TechnitiumScopeRead,
)
from app.services import technitium_dhcp as td
from app.services.background_tasks import spawn_task
from app.services.technitium import TechnitiumError

router = APIRouter(prefix="/technitium-dhcp", tags=["technitium-dhcp"], dependencies=[Depends(require_admin)])


def _meta(request: Request) -> dict[str, Any]:
    return {"actor_ip": request.client.host if request.client else None,
            "actor_user_agent": request.headers.get("user-agent"),
            "request_id": getattr(request.state, "request_id", None)}


def _read(inst: TechnitiumDhcpServer) -> TechnitiumDhcpRead:
    out = TechnitiumDhcpRead.model_validate(inst)
    out.has_token = bool(inst.token_enc)
    return out


async def _or_404(session: AsyncSession, sid: uuid.UUID) -> TechnitiumDhcpServer:
    inst = await session.get(TechnitiumDhcpServer, sid)
    if inst is None:
        raise HTTPException(404, detail="Not found")
    return inst


@router.get("/servers", response_model=Paginated[TechnitiumDhcpRead])
async def list_servers(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(50, ge=1, le=500),
) -> Paginated[TechnitiumDhcpRead]:
    rows = list((await session.execute(select(TechnitiumDhcpServer).order_by(TechnitiumDhcpServer.name)
                                       .offset((page - 1) * page_size).limit(page_size))).scalars().all())
    total = int(await session.scalar(select(func.count()).select_from(TechnitiumDhcpServer)) or 0)
    return Paginated[TechnitiumDhcpRead](items=[_read(r) for r in rows], total=total, page=page,
                                         page_size=page_size)


@router.post("/servers", response_model=TechnitiumDhcpRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    payload: TechnitiumDhcpCreate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TechnitiumDhcpRead:
    inst = TechnitiumDhcpServer(**payload.model_dump(exclude={"token"}))
    session.add(inst)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="Name already exists") from exc
    inst.token_enc, inst.token_nonce = td.encrypt_token(inst.id, payload.token)
    await append_audit(session, actor_user_id=str(user.id), object_type="technitium_dhcp_server",
                       object_id=str(inst.id), action="create",
                       diff={"name": inst.name, "api_url": inst.api_url}, **_meta(request))
    await session.commit()
    await session.refresh(inst)
    return _read(inst)


@router.patch("/servers/{server_id}", response_model=TechnitiumDhcpRead)
async def update_server(
    server_id: uuid.UUID, payload: TechnitiumDhcpUpdate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TechnitiumDhcpRead:
    inst = await _or_404(session, server_id)
    data = payload.model_dump(exclude_unset=True)
    new_token = data.pop("token", None) or None
    for k in ("name", "api_url", "verify_tls", "enabled", "sync_scopes", "sync_leases", "sync_interval_seconds"):
        if k in data and data[k] is None:
            data.pop(k)                         # 不可清空的欄位：送 null 當作沒改
    for k, v in data.items():
        setattr(inst, k, v)
    if new_token:
        inst.token_enc, inst.token_nonce = td.encrypt_token(inst.id, new_token)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="Name already exists") from exc
    diff: dict[str, Any] = {k: ([str(x) for x in v] if isinstance(v, list) else v) for k, v in data.items()}
    if new_token:
        diff["token"] = "changed"   # noqa: S105  稽核只記「換過」，不是 token 本身
    await append_audit(session, actor_user_id=str(user.id), object_type="technitium_dhcp_server",
                       object_id=str(inst.id), action="update", diff=diff, **_meta(request))
    await session.commit()
    await session.refresh(inst)
    return _read(inst)


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(
    server_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    inst = await _or_404(session, server_id)
    # 它寫進共用表的範圍／保留／租約／主機名稱一併收回（範圍鏡像有外鍵 CASCADE）
    from app.services.integration_cleanup import forget_instance
    await forget_instance(session, source=td.SOURCE, source_id=inst.id)
    await session.delete(inst)
    await append_audit(session, actor_user_id=str(user.id), object_type="technitium_dhcp_server",
                       object_id=str(server_id), action="delete", diff={}, **_meta(request))
    await session.commit()


@router.post("/servers/{server_id}/test")
async def test_server(
    server_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    inst = await _or_404(session, server_id)
    try:
        return await td.healthcheck(inst)
    except TechnitiumError as exc:
        raise HTTPException(502, detail=detail_of(exc, "technitium_error")) from exc


@router.post("/servers/{server_id}/sync")
async def sync_server(
    server_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """非同步 —— 立刻回 task_id，同步在背景跑（作業頁看得到）。"""
    inst = await _or_404(session, server_id)
    actor_user_id, inst_name, meta = user.id, inst.name, _meta(request)

    async def _runner(sess: AsyncSession, _task: Any) -> dict[str, Any]:
        obj = await sess.get(TechnitiumDhcpServer, server_id)
        if obj is None:
            raise RuntimeError("Technitium DHCP server disappeared")
        summary = await td.sync_instance(sess, obj)
        await append_audit(sess, actor_user_id=str(actor_user_id), object_type="technitium_dhcp_server",
                           object_id=str(server_id), action="sync", diff=summary, **meta)
        await sess.commit()
        return summary

    task = await spawn_task(session=session, kind="technitium_dhcp.sync", target_type="technitium_dhcp_server",
                            target_id=server_id, target_label=inst_name, actor_user_id=actor_user_id,
                            runner=_runner)
    return {"task_id": str(task.id), "status": task.status, "queued_at": task.queued_at.isoformat()}


@router.get("/servers/{server_id}/scopes", response_model=list[TechnitiumScopeRead])
async def list_scopes(
    server_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TechnitiumScopeRead]:
    """上一次同步看到的範圍（發放範圍、排除區間、發給用戶端的閘道／DNS／NTP／WINS）。"""
    await _or_404(session, server_id)
    rows = (await session.execute(select(TechnitiumDhcpScope).where(TechnitiumDhcpScope.server_id == server_id)
                                  .order_by(TechnitiumDhcpScope.name))).scalars().all()
    return [TechnitiumScopeRead(
        name=r.name, enabled=r.enabled, subnet_cidr=r.subnet_cidr, start_ip=r.start_ip, end_ip=r.end_ip,
        exclusions=list(r.exclusions or []), router=str(r.router) if r.router else None,
        dns_servers=[str(x) for x in r.dns_servers or []], ntp_servers=[str(x) for x in r.ntp_servers or []],
        wins_servers=[str(x) for x in r.wins_servers or []], domain_name=r.domain_name,
        lease_seconds=r.lease_seconds, reservations=r.reservations, synced_at=r.synced_at) for r in rows]
