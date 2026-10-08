"""Check Point 第二階段 endpoints（admin only）：閘道的 Gaia API 連線設定、測試、手動同步、DHCP 子網路鏡像。

一筆＝一台閘道，掛在某台管理伺服器（第一階段）底下。唯讀讀 DHCP 設定；
ARP 表與租約靠 `allow_scripts`（預設開；帳號不能執行指令時同步記成略過，見 services/checkpoint_gaia.py）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import CurrentUser, require_admin
from app.core.audit import append_audit
from app.core.db import get_session
from app.core.ui_error import detail_of
from app.models.checkpoint import CheckPointServer
from app.models.checkpoint_gaia import CheckPointDhcpSubnet, CheckPointGaiaTarget
from app.schemas.checkpoint_gaia import GaiaTargetCreate, GaiaTargetRead, GaiaTargetUpdate
from app.services import checkpoint_gaia as cpg
from app.services.background_tasks import spawn_task

router = APIRouter(prefix="/checkpoint", tags=["checkpoint"], dependencies=[Depends(require_admin)])


def _meta(request: Request) -> dict[str, Any]:
    return {"actor_ip": request.client.host if request.client else None,
            "actor_user_agent": request.headers.get("user-agent"),
            "request_id": getattr(request.state, "request_id", None)}


def _read(t: CheckPointGaiaTarget) -> GaiaTargetRead:
    out = GaiaTargetRead.model_validate(t)
    out.has_secret = bool(t.secret_enc)
    return out


async def _target_or_404(session: AsyncSession, tid: uuid.UUID) -> CheckPointGaiaTarget:
    t = await session.get(CheckPointGaiaTarget, tid)
    if t is None:
        raise HTTPException(404, detail="Not found")
    return t


@router.get("/servers/{server_id}/gaia-targets", response_model=list[GaiaTargetRead])
async def list_targets(server_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> list[GaiaTargetRead]:
    if await session.get(CheckPointServer, server_id) is None:
        raise HTTPException(404, detail="Not found")
    rows = (await session.execute(select(CheckPointGaiaTarget).where(CheckPointGaiaTarget.server_id == server_id)
                                  .order_by(CheckPointGaiaTarget.name))).scalars().all()
    return [_read(r) for r in rows]


@router.post("/servers/{server_id}/gaia-targets", response_model=GaiaTargetRead, status_code=status.HTTP_201_CREATED)
async def create_target(
    server_id: uuid.UUID, payload: GaiaTargetCreate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GaiaTargetRead:
    if await session.get(CheckPointServer, server_id) is None:
        raise HTTPException(404, detail="Not found")
    t = CheckPointGaiaTarget(server_id=server_id, **payload.model_dump(exclude={"secret"}))
    session.add(t)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="Name already exists") from exc
    t.secret_enc, t.secret_nonce = cpg.encrypt_secret_for(t.id, payload.secret)
    await append_audit(session, actor_user_id=str(user.id), object_type="checkpoint_gaia_target", object_id=str(t.id),
                       action="create", diff={"name": t.name, "gaia_url": t.gaia_url, "username": t.username,
                                              "allow_scripts": t.allow_scripts}, **_meta(request))
    await session.commit()
    await session.refresh(t)
    return _read(t)


@router.patch("/gaia-targets/{target_id}", response_model=GaiaTargetRead)
async def update_target(
    target_id: uuid.UUID, payload: GaiaTargetUpdate, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GaiaTargetRead:
    t = await _target_or_404(session, target_id)
    data = payload.model_dump(exclude_unset=True)
    new_secret = data.pop("secret", None) or None
    for k in ("name", "gaia_url", "username", "verify_tls", "enabled", "sync_interval_seconds", "sync_dhcp",
              "allow_scripts", "sync_arp", "sync_leases"):
        if k in data and data[k] is None:
            data.pop(k)
    for k, v in data.items():
        setattr(t, k, v)
    if new_secret:
        t.secret_enc, t.secret_nonce = cpg.encrypt_secret_for(t.id, new_secret)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, detail="Name already exists") from exc
    diff: dict[str, Any] = {k: ([str(x) for x in v] if isinstance(v, list) else v) for k, v in data.items()}
    if new_secret:
        diff["secret"] = "changed"   # noqa: S105  稽核只記「換過」，不是密碼本身
    await append_audit(session, actor_user_id=str(user.id), object_type="checkpoint_gaia_target", object_id=str(t.id),
                       action="update", diff=diff, **_meta(request))
    await session.commit()
    await session.refresh(t)
    return _read(t)


@router.delete("/gaia-targets/{target_id}", status_code=204)
async def delete_target(
    target_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    t = await _target_or_404(session, target_id)
    # 它寫進共用表的發放範圍、租約旗標、主機名稱一併收回（子網路鏡像有外鍵 CASCADE）
    from app.services.integration_cleanup import forget_instance
    await forget_instance(session, source=cpg.SOURCE, source_id=t.id)
    await session.delete(t)
    await append_audit(session, actor_user_id=str(user.id), object_type="checkpoint_gaia_target",
                       object_id=str(target_id), action="delete", diff={}, **_meta(request))
    await session.commit()


@router.post("/gaia-targets/{target_id}/test")
async def test_target(target_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    t = await _target_or_404(session, target_id)
    try:
        return await cpg.diagnose(t)
    except cpg.GaiaError as exc:
        raise HTTPException(502, detail=detail_of(exc, "cpg_api_error")) from exc


@router.post("/gaia-targets/{target_id}/sync")
async def sync_target(
    target_id: uuid.UUID, user: CurrentUser, request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """非同步 —— 立刻回 task_id，同步在背景跑（作業頁看得到）。"""
    t = await _target_or_404(session, target_id)
    actor_user_id, name, meta = user.id, t.name, _meta(request)

    async def _runner(sess: AsyncSession, _task: Any) -> dict[str, Any]:
        obj = await sess.get(CheckPointGaiaTarget, target_id)
        if obj is None:
            raise RuntimeError("Check Point gateway disappeared")
        summary = await cpg.sync_target(sess, obj)
        await append_audit(sess, actor_user_id=str(actor_user_id), object_type="checkpoint_gaia_target",
                           object_id=str(target_id), action="sync",
                           diff={k: v for k, v in summary.items() if k != "errors"}, **meta)
        await sess.commit()
        return summary

    task = await spawn_task(session=session, kind="checkpoint_gaia.sync", target_type="checkpoint_gaia_target",
                            target_id=target_id, target_label=name, actor_user_id=actor_user_id, runner=_runner)
    return {"task_id": str(task.id), "status": task.status, "queued_at": task.queued_at.isoformat()}


@router.get("/gaia-targets/{target_id}/dhcp")
async def list_dhcp(target_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> list[dict[str, Any]]:
    await _target_or_404(session, target_id)
    rows = (await session.execute(select(CheckPointDhcpSubnet).where(CheckPointDhcpSubnet.target_id == target_id)
                                  .order_by(CheckPointDhcpSubnet.subnet_cidr))).scalars().all()
    return [{"subnet_cidr": r.subnet_cidr, "enabled": r.enabled,
             "default_gateway": str(r.default_gateway) if r.default_gateway else None,
             "dns_servers": [str(x) for x in (r.dns_servers or [])], "domain_name": r.domain_name,
             "default_lease": r.default_lease, "max_lease": r.max_lease, "pools": r.pools or [],
             "synced_at": r.synced_at.isoformat() if r.synced_at else None} for r in rows]
