"""Check Point endpoints（admin only）：管理伺服器設定、測試連線、手動同步、規則／物件／閘道的唯讀清單。

第一階段只讀 Management API；寫法比照 Palo Alto／Kea。實機驗收待 VM（標 Beta）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.ui_error import detail_of
from app.models.checkpoint import (
    CheckPointGateway,
    CheckPointObject,
    CheckPointRule,
    CheckPointServer,
)
from app.schemas.base import Paginated
from app.schemas.checkpoint import CheckPointCreate, CheckPointRead, CheckPointUpdate
from app.services import checkpoint as cp
from app.services.background_tasks import spawn_task

router = APIRouter(prefix="/checkpoint", tags=["checkpoint"], dependencies=[Depends(require_admin)])


def _meta(request: Request) -> dict[str, Any]:
    return {"actor_ip": request.client.host if request.client else None,
            "actor_user_agent": request.headers.get("user-agent"),
            "request_id": getattr(request.state, "request_id", None)}


def _read(inst: CheckPointServer) -> CheckPointRead:
    out = CheckPointRead.model_validate(inst)
    out.has_secret = bool(inst.secret_enc)
    return out


async def _or_404(session: AsyncSession, sid: uuid.UUID) -> CheckPointServer:
    inst = await session.get(CheckPointServer, sid)
    if inst is None:
        raise HTTPException(404, detail="Not found")
    return inst


@router.get("/servers", response_model=Paginated[CheckPointRead])
async def list_servers(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: int = Query(1, ge=1, le=10_000), page_size: int = Query(50, ge=1, le=500),
) -> Paginated[CheckPointRead]:
    rows = list((await session.execute(select(CheckPointServer).order_by(CheckPointServer.name)
                                       .offset((page - 1) * page_size).limit(page_size))).scalars().all())
    total = int(await session.scalar(select(func.count()).select_from(CheckPointServer)) or 0)
    return Paginated[CheckPointRead](items=[_read(r) for r in rows], total=total, page=page, page_size=page_size)


@router.post("/servers", response_model=CheckPointRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    payload: CheckPointCreate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CheckPointRead:
    inst = CheckPointServer(**payload.model_dump(exclude={"secret"}))
    session.add(inst)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="Name already exists") from exc
    inst.secret_enc, inst.secret_nonce = cp.encrypt_secret_for(inst.id, payload.secret)
    await append_audit(session, actor_user_id=str(user.id), object_type="checkpoint_server", object_id=str(inst.id),
                       action="create", diff={"name": inst.name, "api_url": inst.api_url, "auth_mode": inst.auth_mode},
                       **_meta(request))
    await session.commit()
    await session.refresh(inst)
    return _read(inst)


@router.patch("/servers/{server_id}", response_model=CheckPointRead)
async def update_server(
    server_id: uuid.UUID, payload: CheckPointUpdate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CheckPointRead:
    inst = await _or_404(session, server_id)
    data = payload.model_dump(exclude_unset=True)
    new_secret = data.pop("secret", None) or None
    for k in ("name", "api_url", "verify_tls", "auth_mode", "enabled", "sync_interval_seconds", "sync_objects",
              "sync_policies", "sync_nat"):
        if k in data and data[k] is None:
            data.pop(k)
    for k, v in data.items():
        setattr(inst, k, v)
    if new_secret:
        inst.secret_enc, inst.secret_nonce = cp.encrypt_secret_for(inst.id, new_secret)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="Name already exists") from exc
    diff: dict[str, Any] = {k: ([str(x) for x in v] if isinstance(v, list) else v) for k, v in data.items()}
    if new_secret:
        diff["secret"] = "changed"   # noqa: S105  稽核只記「換過」，不是密鑰本身
    await append_audit(session, actor_user_id=str(user.id), object_type="checkpoint_server", object_id=str(inst.id),
                       action="update", diff=diff, **_meta(request))
    await session.commit()
    await session.refresh(inst)
    return _read(inst)


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(
    server_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    inst = await _or_404(session, server_id)
    # 它寫進共用表的 NAT 一併收回（鏡像表有外鍵 CASCADE）
    from app.services.integration_cleanup import forget_instance
    await forget_instance(session, source=cp.SOURCE, source_id=inst.id)
    # 第二階段：它底下每台閘道（Gaia API）寫進共用表的發放範圍、租約旗標、主機名稱
    from app.services.checkpoint_gaia import forget_server_targets
    await forget_server_targets(session, inst.id)
    await session.delete(inst)
    await append_audit(session, actor_user_id=str(user.id), object_type="checkpoint_server", object_id=str(server_id),
                       action="delete", diff={}, **_meta(request))
    await session.commit()


@router.post("/servers/{server_id}/test")
async def test_server(server_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    inst = await _or_404(session, server_id)
    try:
        return await cp.diagnose(inst)
    except cp.CheckPointError as exc:
        raise HTTPException(502, detail=detail_of(exc, "cp_api_error")) from exc


@router.post("/servers/{server_id}/sync")
async def sync_server(
    server_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """非同步 —— 立刻回 task_id，同步在背景跑（作業頁看得到）。"""
    inst = await _or_404(session, server_id)
    actor_user_id, inst_name, meta = user.id, inst.name, _meta(request)

    async def _runner(sess: AsyncSession, _task: Any) -> dict[str, Any]:
        obj = await sess.get(CheckPointServer, server_id)
        if obj is None:
            raise RuntimeError("Check Point server disappeared")
        summary = await cp.sync_instance(sess, obj)
        await append_audit(sess, actor_user_id=str(actor_user_id), object_type="checkpoint_server",
                           object_id=str(server_id), action="sync",
                           diff={k: v for k, v in summary.items() if k != "errors"}, **meta)
        await sess.commit()
        return summary

    task = await spawn_task(session=session, kind="checkpoint.sync", target_type="checkpoint_server",
                            target_id=server_id, target_label=inst_name, actor_user_id=actor_user_id, runner=_runner)
    return {"task_id": str(task.id), "status": task.status, "queued_at": task.queued_at.isoformat()}


@router.get("/servers/{server_id}/rules")
async def list_rules(
    server_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(None, max_length=200), page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(100, ge=1, le=500),
    rule_id: uuid.UUID | None = Query(None, description="只回這一筆（IP 詳細資料點進來）"),
) -> dict[str, Any]:
    await _or_404(session, server_id)
    cond = [CheckPointRule.server_id == server_id]
    if rule_id is not None:
        cond.append(CheckPointRule.id == rule_id)
    if q:
        like = f"%{q.strip()}%"
        cond.append(or_(CheckPointRule.name.ilike(like), CheckPointRule.source.ilike(like),
                        CheckPointRule.destination.ilike(like), CheckPointRule.service.ilike(like),
                        CheckPointRule.layer.ilike(like), CheckPointRule.comments.ilike(like)))
    total = int(await session.scalar(select(func.count()).select_from(CheckPointRule).where(*cond)) or 0)
    rows = (await session.execute(select(CheckPointRule).where(*cond).order_by(
        CheckPointRule.domain, CheckPointRule.package, CheckPointRule.layer, CheckPointRule.rule_number)
        .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "page": page, "page_size": page_size, "items": [{
        "id": str(r.id), "domain": r.domain, "package": r.package, "layer": r.layer, "section": r.section,
        "rule_number": r.rule_number, "name": r.name, "action": r.action, "enabled": r.enabled, "source": r.source,
        "destination": r.destination, "service": r.service, "source_negate": r.source_negate,
        "destination_negate": r.destination_negate, "install_on": r.install_on, "comments": r.comments,
        "hits": r.hits, "last_hit_at": r.last_hit_at.isoformat() if r.last_hit_at else None} for r in rows]}


@router.get("/servers/{server_id}/objects")
async def list_objects(
    server_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(None, max_length=200), page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(100, ge=1, le=500),
    name: str | None = Query(None, max_length=255, description="只回這個名稱（IP 詳細資料點進來）"),
) -> dict[str, Any]:
    await _or_404(session, server_id)
    cond = [CheckPointObject.server_id == server_id]
    if name:
        cond.append(CheckPointObject.name == name)
    if q:
        like = f"%{q.strip()}%"
        cond.append(or_(CheckPointObject.name.ilike(like), CheckPointObject.value.ilike(like),
                        CheckPointObject.comments.ilike(like)))
    total = int(await session.scalar(select(func.count()).select_from(CheckPointObject).where(*cond)) or 0)
    rows = (await session.execute(select(CheckPointObject).where(*cond).order_by(
        CheckPointObject.domain, CheckPointObject.name).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "page": page, "page_size": page_size, "items": [{
        "id": str(o.id), "domain": o.domain, "name": o.name, "type": o.obj_type, "value": o.value,
        "members": o.members or [], "comments": o.comments} for o in rows]}


@router.get("/servers/{server_id}/gateways")
async def list_gateways(server_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> list[dict[str, Any]]:
    await _or_404(session, server_id)
    rows = (await session.execute(select(CheckPointGateway).where(CheckPointGateway.server_id == server_id)
                                  .order_by(CheckPointGateway.domain, CheckPointGateway.name))).scalars().all()
    return [{"domain": g.domain, "uid": g.uid, "name": g.name, "type": g.gw_type,
             "ipv4_address": str(g.ipv4_address) if g.ipv4_address else None, "version": g.version} for g in rows]
